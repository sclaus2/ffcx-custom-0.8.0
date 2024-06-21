# Copyright (C) 2015-2024 Martin Sandve Alnæs, Michal Habera, Igor Baratta, Chris Richardson
#
# Modified by Jørgen S. Dokken, 2024
#
# This file is part of FFCx. (https://www.fenicsproject.org)
#
# SPDX-License-Identifier:    LGPL-3.0-or-later
"""Integral generator."""

import collections
import logging
from numbers import Integral
from typing import Any

import ufl

import ffcx.codegeneration.lnodes as L
from ffcx.codegeneration import geometry
from ffcx.codegeneration.definitions import create_dof_index, create_quadrature_index
from ffcx.codegeneration.optimizer import optimize
from ffcx.ir.integral import BlockDataT
from ffcx.ir.representationutils import QuadratureRule

logger = logging.getLogger("ffcx")


def extract_dtype(v, vops: list[Any]):
    """Extract dtype from ufl expression v and its operands."""
    dtypes = []
    for op in vops:
        if hasattr(op, "dtype"):
            dtypes.append(op.dtype)
        elif hasattr(op, "symbol"):
            dtypes.append(op.symbol.dtype)
        elif isinstance(op, Integral):
            dtypes.append(L.DataType.INT)
        else:
            raise RuntimeError(f"Not expecting this type of operand {type(op)}")
    is_cond = isinstance(v, ufl.classes.Condition)
    return L.DataType.BOOL if is_cond else L.merge_dtypes(dtypes)


class IntegralGenerator:
    """Integral generator."""

    def __init__(self, ir, backend):
        """Initialise."""
        # Store ir
        self.ir = ir

        # Backend specific plugin with attributes
        # - symbols: for translating ufl operators to target language
        # - definitions: for defining backend specific variables
        # - access: for accessing backend specific variables
        self.backend = backend

        # Set of operator names code has been generated for, used in the
        # end for selecting necessary includes
        self._ufl_names = set()

        # Initialize lookup tables for variable scopes
        self.init_scopes()

        # Cache
        self.temp_symbols = {}

        # Set of counters used for assigning names to intermediate
        # variables
        self.symbol_counters = collections.defaultdict(int)

    def init_scopes(self):
        """Initialize variable scope dicts."""
        # Reset variables, separate sets for each quadrature rule
        self.scopes = {quadrature_rule: {} for quadrature_rule in self.ir.integrand.keys()}
        self.scopes[None] = {}

    def set_var(self, quadrature_rule, v, vaccess):
        """Set a new variable in variable scope dicts.

        Scope is determined by quadrature_rule which identifies the
        quadrature loop scope or None if outside quadrature loops.

        Args:
            quadrature_rule: Quadrature rule
            v: the ufl expression
            vaccess: the LNodes expression to access the value in the code
        """
        self.scopes[quadrature_rule][v] = vaccess

    def get_var(self, quadrature_rule, v):
        """Lookup ufl expression v in variable scope dicts.

        Scope is determined by quadrature rule which identifies the
        quadrature loop scope or None if outside quadrature loops.

        If v is not found in quadrature loop scope, the piecewise
        scope (None) is checked.

        Returns the LNodes expression to access the value in the code.
        """
        if v._ufl_is_literal_:
            return L.ufl_to_lnodes(v)

        # quadrature loop scope
        f = self.scopes[quadrature_rule].get(v)

        # piecewise scope
        if f is None:
            f = self.scopes[None].get(v)
        return f

    def new_temp_symbol(self, basename):
        """Create a new code symbol named basename + running counter."""
        name = "%s%d" % (basename, self.symbol_counters[basename])
        self.symbol_counters[basename] += 1
        return L.Symbol(name, dtype=L.DataType.SCALAR)

    def get_temp_symbol(self, tempname, key):
        """Get a temporary symbol."""
        key = (tempname,) + key
        s = self.temp_symbols.get(key)
        defined = s is not None
        if not defined:
            s = self.new_temp_symbol(tempname)
            self.temp_symbols[key] = s
        return s, defined

    def generate(self):
        """Generate entire tabulate_tensor body.

        Assumes that the code returned from here will be wrapped in a
        context that matches a suitable version of the UFC
        tabulate_tensor signatures.
        """
        # Assert that scopes are empty: expecting this to be called only
        # once
        assert not any(d for d in self.scopes.values())

        parts = []

        # Generate the tables of quadrature points and weights
        parts += self.generate_quadrature_tables()

        # Generate the tables of basis function values and
        # pre-integrated blocks
        parts += self.generate_element_tables()

        # Generate the tables of geometry data that are needed
        parts += self.generate_geometry_tables()

        # Loop generation code will produce parts to go before
        # quadloops, to define the quadloops, and to go after the
        # quadloops
        all_preparts = []
        all_quadparts = []

        # Pre-definitions are collected across all quadrature loops to
        # improve re-use and avoid name clashes
        for rule in self.ir.integrand.keys():
            # Generate code to compute piecewise constant scalar factors
            all_preparts += self.generate_piecewise_partition(rule)

            # Generate code to integrate reusable blocks of final
            # element tensor
            all_quadparts += self.generate_quadrature_loop(rule)

        # Collect parts before, during, and after quadrature loops
        parts += all_preparts
        parts += all_quadparts

        return L.StatementList(parts)

    def generate_quadrature_tables(self):
        """Generate static tables of quadrature points and weights."""
        parts = []

        # No quadrature tables for custom (given argument) or point
        # (evaluation in single vertex)
        skip = ufl.custom_integral_types + ufl.measure.point_integral_types
        if self.ir.integral_type in skip:
            return parts

        # Loop over quadrature rules
        for quadrature_rule, integrand in self.ir.integrand.items():
            # Generate quadrature weights array
            wsym = self.backend.symbols.weights_table(quadrature_rule)
            parts += [L.ArrayDecl(wsym, values=quadrature_rule.weights, const=True)]

        # Add leading comment if there are any tables
        parts = L.commented_code_list(parts, "Quadrature rules")
        return parts

    def generate_geometry_tables(self):
        """Generate static tables of geometry data."""
        ufl_geometry = {
            ufl.geometry.FacetEdgeVectors: "facet_edge_vertices",
            ufl.geometry.CellFacetJacobian: "reference_facet_jacobian",
            ufl.geometry.ReferenceCellVolume: "reference_cell_volume",
            ufl.geometry.ReferenceFacetVolume: "reference_facet_volume",
            ufl.geometry.ReferenceCellEdgeVectors: "reference_edge_vectors",
            ufl.geometry.ReferenceFacetEdgeVectors: "facet_reference_edge_vectors",
            ufl.geometry.ReferenceNormal: "reference_facet_normals",
            ufl.geometry.FacetOrientation: "facet_orientation",
        }
        cells: dict[Any, set[Any]] = {t: set() for t in ufl_geometry.keys()}  # type: ignore

        for integrand in self.ir.integrand.values():
            for attr in integrand["factorization"].nodes.values():
                mt = attr.get("mt")
                if mt is not None:
                    t = type(mt.terminal)
                    if t in ufl_geometry:
                        cells[t].add(
                            ufl.domain.extract_unique_domain(mt.terminal).ufl_cell().cellname()
                        )

        parts = []
        for i, cell_list in cells.items():
            for c in cell_list:
                parts.append(geometry.write_table(ufl_geometry[i], c))

        return parts

    def generate_element_tables(self):
        """Generate static tables.

        With precomputed element basis function values in quadrature points.
        """
        parts = []
        tables = self.ir.unique_tables

        # Define all tables
        table_names = sorted(tables)

        if self.ir.integral_type in ufl.custom_integral_types:
            #table_names = [name for name in sorted(tables) if table_types[name] in piecewise_ttypes]
            element_tables = self.ir.unique_element_tables
            #element_deriv_order = self.ir.element_deriv_order

            el_def_part, tab_part, copy_table_part = self.generate_custom_integral_tables(element_tables, tables)
            parts += el_def_part
            parts += tab_part
            parts += copy_table_part

            #Register table name as element_tables symbol
            for name in table_names:
              table_symbol = L.Symbol(name, dtype=L.DataType.REAL)
              self.backend.symbols.element_tables[name] = table_symbol

        else:
           for name in table_names:
              table = tables[name]
              parts += self.declare_table(name, table)

        # Add leading comment if there are any tables
        parts = L.commented_code_list(
            parts,
            [
                "Precomputed values of basis functions and precomputations",
                "FE* dimensions: [permutation][entities][points][dofs]",
            ],
        )
        return parts

    def declare_table(self, name, table):
        """Declare a table.

        If the dof dimensions of the table have dof rotations, apply
        these rotations.

        """
        table_symbol = L.Symbol(name, dtype=L.DataType.REAL)
        self.backend.symbols.element_tables[name] = table_symbol
        return [L.ArrayDecl(table_symbol, values=table, const=True)]

    def generate_quadrature_loop(self, quadrature_rule: QuadratureRule):
        """Generate quadrature loop with for this quadrature_rule."""
        # Generate varying partition
        definitions, intermediates_0 = self.generate_varying_partition(quadrature_rule)

        # Generate dofblock parts, some of this will be placed before or after quadloop
        tensor_comp, intermediates_fw = self.generate_dofblock_partition(quadrature_rule)
        assert all([isinstance(tc, L.Section) for tc in tensor_comp])

        # Check if we only have Section objects
        inputs = []
        for definition in definitions:
            assert isinstance(definition, L.Section)
            inputs += definition.output

        # Create intermediates section
        output = []
        declarations = []
        for fw in intermediates_fw:
            assert isinstance(fw, L.VariableDecl)
            output += [fw.symbol]
            declarations += [L.VariableDecl(fw.symbol, 0)]
            intermediates_0 += [L.Assign(fw.symbol, fw.value)]
        intermediates = [L.Section("Intermediates", intermediates_0, declarations, inputs, output)]

        iq_symbol = self.backend.symbols.quadrature_loop_index
        iq = create_quadrature_index(quadrature_rule, iq_symbol)

        if self.ir.integral_type in ufl.custom_integral_types:
            iq.sizes[0] = self.backend.symbols.custom_num_points()

        code = definitions + intermediates + tensor_comp
        code = optimize(code, quadrature_rule)

        return [L.create_nested_for_loops([iq], code)]

    def generate_piecewise_partition(self, quadrature_rule):
        """Generate a piecewise partition."""
        # Get annotated graph of factorisation
        F = self.ir.integrand[quadrature_rule]["factorization"]
        arraysymbol = L.Symbol(f"sp_{quadrature_rule.id()}", dtype=L.DataType.SCALAR)
        return self.generate_partition(arraysymbol, F, "piecewise", None)

    def generate_varying_partition(self, quadrature_rule):
        """Generate a varying partition."""
        # Get annotated graph of factorisation
        F = self.ir.integrand[quadrature_rule]["factorization"]
        arraysymbol = L.Symbol(f"sv_{quadrature_rule.id()}", dtype=L.DataType.SCALAR)
        return self.generate_partition(arraysymbol, F, "varying", quadrature_rule)

    def generate_partition(self, symbol, F, mode, quadrature_rule):
        """Generate a partition."""
        definitions = []
        intermediates = []

        for i, attr in F.nodes.items():
            if attr["status"] != mode:
                continue
            v = attr["expression"]

            # Generate code only if the expression is not already in cache
            if not self.get_var(quadrature_rule, v):
                if v._ufl_is_literal_:
                    vaccess = L.ufl_to_lnodes(v)
                elif mt := attr.get("mt"):
                    tabledata = attr.get("tr")

                    # Backend specific modified terminal translation
                    vaccess = self.backend.access.get(mt, tabledata, quadrature_rule)
                    vdef = self.backend.definitions.get(mt, tabledata, quadrature_rule, vaccess)

                    if vdef:
                        assert isinstance(vdef, L.Section)
                    # Only add if definition is unique.
                    # This can happen when using sub-meshes
                    if vdef not in definitions:
                        definitions += [vdef]
                else:
                    # Get previously visited operands
                    vops = [self.get_var(quadrature_rule, op) for op in v.ufl_operands]
                    dtype = extract_dtype(v, vops)

                    # Mapping UFL operator to target language
                    self._ufl_names.add(v._ufl_handler_name_)
                    vexpr = L.ufl_to_lnodes(v, *vops)

                    j = len(intermediates)
                    vaccess = L.Symbol(f"{symbol.name}_{j}", dtype=dtype)
                    intermediates.append(L.VariableDecl(vaccess, vexpr))

                # Store access node for future reference
                self.set_var(quadrature_rule, v, vaccess)

        # Optimize definitions
        definitions = optimize(definitions, quadrature_rule)
        return definitions, intermediates

    def generate_dofblock_partition(self, quadrature_rule: QuadratureRule):
        """Generate a dofblock partition."""
        block_contributions = self.ir.integrand[quadrature_rule]["block_contributions"]
        quadparts = []
        blocks = [
            (blockmap, blockdata)
            for blockmap, contributions in sorted(block_contributions.items())
            for blockdata in contributions
        ]

        block_groups = collections.defaultdict(list)

        # Group loops by blockmap, in Vector elements each component has
        # a different blockmap
        for blockmap, blockdata in blocks:
            scalar_blockmap = []
            assert len(blockdata.ma_data) == len(blockmap)
            for i, b in enumerate(blockmap):
                bs = blockdata.ma_data[i].tabledata.block_size
                offset = blockdata.ma_data[i].tabledata.offset
                b = tuple([(idx - offset) // bs for idx in b])
                scalar_blockmap.append(b)
            block_groups[tuple(scalar_blockmap)].append(blockdata)

        intermediates = []
        for blockmap in block_groups:
            block_quadparts, intermediate = self.generate_block_parts(
                quadrature_rule, blockmap, block_groups[blockmap]
            )
            intermediates += intermediate

            # Add computations
            quadparts.extend(block_quadparts)

        return quadparts, intermediates

    def get_arg_factors(self, blockdata, block_rank, quadrature_rule, iq, indices):
        """Get arg factors."""
        arg_factors = []
        tables = []
        for i in range(block_rank):
            mad = blockdata.ma_data[i]
            td = mad.tabledata
            scope = self.ir.integrand[quadrature_rule]["modified_arguments"]
            mt = scope[mad.ma_index]
            arg_tables = []

            # Translate modified terminal to code
            # TODO: Move element table access out of backend?
            #       Not using self.backend.access.argument() here
            #       now because it assumes too much about indices.

            assert td.ttype != "zeros"

            if td.ttype == "ones":
                arg_factor = 1
            else:
                # Assuming B sparsity follows element table sparsity
                arg_factor, arg_tables = self.backend.access.table_access(
                    td, self.ir.entitytype, mt.restriction, iq, indices[i]
                )

            tables += arg_tables
            arg_factors.append(arg_factor)

        return arg_factors, tables

    def generate_block_parts(
        self, quadrature_rule: QuadratureRule, blockmap: tuple, blocklist: list[BlockDataT]
    ):
        """Generate and return code parts for a given block.

        Returns parts occurring before, inside, and after the quadrature
        loop identified by the quadrature rule.

        Should be called with quadrature_rule=None for
        quadloop-independent blocks.
        """
        # The parts to return
        quadparts: list[L.LNode] = []
        intermediates: list[L.LNode] = []
        tables = []
        vars = []

        # RHS expressions grouped by LHS "dofmap"
        rhs_expressions = collections.defaultdict(list)

        block_rank = len(blockmap)
        iq_symbol = self.backend.symbols.quadrature_loop_index
        iq = create_quadrature_index(quadrature_rule, iq_symbol)

        for blockdata in blocklist:
            B_indices = []
            for i in range(block_rank):
                table_ref = blockdata.ma_data[i].tabledata
                symbol = self.backend.symbols.argument_loop_index(i)
                index = create_dof_index(table_ref, symbol)
                B_indices.append(index)

            ttypes = blockdata.ttypes
            if "zeros" in ttypes:
                raise RuntimeError(
                    "Not expecting zero arguments to be left in dofblock generation."
                )

            if len(blockdata.factor_indices_comp_indices) > 1:
                raise RuntimeError("Code generation for non-scalar integrals unsupported")

            # We have scalar integrand here, take just the factor index
            factor_index = blockdata.factor_indices_comp_indices[0][0]

            # Get factor expression
            F = self.ir.integrand[quadrature_rule]["factorization"]

            v = F.nodes[factor_index]["expression"]
            f = self.get_var(quadrature_rule, v)

            # Quadrature weight was removed in representation, add it back now
            if self.ir.integral_type in ufl.custom_integral_types:
                weights = self.backend.symbols.custom_weights_table
                weight = weights[iq.global_index]
            else:
                weights = self.backend.symbols.weights_table(quadrature_rule)
                weight = weights[iq.global_index]

            # Define fw = f * weight
            fw_rhs = L.float_product([f, weight])
            if not isinstance(fw_rhs, L.Product):
                fw = fw_rhs
            else:
                # Define and cache scalar temp variable
                key = (quadrature_rule, factor_index, blockdata.all_factors_piecewise)
                fw, defined = self.get_temp_symbol("fw", key)
                if not defined:
                    input = [f, weight]
                    # filter only L.Symbol in input
                    input = [i for i in input if isinstance(i, L.Symbol)]
                    output = [fw]

                    # assert input and output are Symbol objects
                    assert all(isinstance(i, L.Symbol) for i in input)
                    assert all(isinstance(o, L.Symbol) for o in output)

                    intermediates += [L.VariableDecl(fw, fw_rhs)]

            var = fw if isinstance(fw, L.Symbol) else fw.array
            vars += [var]
            assert not blockdata.transposed, "Not handled yet"

            # Fetch code to access modified arguments
            arg_factors, table = self.get_arg_factors(
                blockdata, block_rank, quadrature_rule, iq, B_indices
            )
            tables += table

            # Define B_rhs = fw * arg_factors
            B_rhs = L.float_product([fw] + arg_factors)

            A_indices = []
            for i in range(block_rank):
                index = B_indices[i]
                tabledata = blockdata.ma_data[i].tabledata
                offset = tabledata.offset
                if len(blockmap[i]) == 1:
                    A_indices.append(index.global_index + offset)
                else:
                    block_size = blockdata.ma_data[i].tabledata.block_size
                    A_indices.append(block_size * index.global_index + offset)
            rhs_expressions[tuple(A_indices)].append(B_rhs)

        # List of statements to keep in the inner loop
        keep = collections.defaultdict(list)

        for indices in rhs_expressions:
            keep[indices] = rhs_expressions[indices]

        body: list[L.LNode] = []

        A = self.backend.symbols.element_tensor
        A_shape = self.ir.tensor_shape
        for indices in keep:
            multi_index = L.MultiIndex(list(indices), A_shape)
            for expression in keep[indices]:
                body.append(L.AssignAdd(A[multi_index], expression))

        # reverse B_indices
        B_indices = B_indices[::-1]
        body = [L.create_nested_for_loops(B_indices, body)]
        input = [*vars, *tables]
        output = [A]

        # Make sure we don't have repeated symbols in input
        input = list(set(input))

        # assert input and output are Symbol objects
        assert all(isinstance(i, L.Symbol) for i in input)
        assert all(isinstance(o, L.Symbol) for o in output)

        annotations = []
        if len(B_indices) > 1:
            annotations.append(L.Annotation.licm)

        quadparts += [L.Section("Tensor Computation", body, [], input, output, annotations)]

        return quadparts, intermediates

    def generate_custom_integral_tables(self, element_tables, tables):
        element_def_parts = []

        #todo: remove self.ir.finite_elements
        #todo: make sure that each element is only genererated once
        num_fe = len(element_tables)
        decl = ""
        if(num_fe>0):
          decl += "basix_element* elements[" + str(num_fe) + "]; \n"

        element_def_parts += [L.LiteralString(decl)]

        for id, e in element_tables.items():
            component_element, _, _ = e.element.get_component_element(e.fc)
            decl = "// Represented element component is " + e.component_element_name + "\n"
            decl += "// index in finite element list "+ str(self.ir.finite_elements.index(e.component_element_name)) + "\n"
            decl += "elements[" + str(id) + "] = basix_element_create("
            decl += str(int(component_element.element_family)) + ", "
            decl += str(int(component_element.cell_type)) + ", "
            decl += str(component_element.degree) + ", "
            decl += str(int(component_element.lagrange_variant)) + ", "
            decl += str(int(component_element.dpc_variant)) + ", "
            decl+= "true" if component_element.discontinuous else "false"
            decl += ");\n"

            element_def_parts += [L.LiteralString(decl)]

        #     element_def_parts += [L.LiteralString(decl)] #[L.VerbatimStatement(decl)]
        #comment = "FIXME: the elements should be generated in another code block for efficiency"
        #element_def_parts = L.commented_code_list(element_def_parts,
        #                                [comment])

        tabulate_parts = []

        decl = "int gdim = " + str(self.ir.geometric_dimension) + ";\n"

        for id, e in element_tables.items():
            nd = e.deriv_order #self.ir.element_deriv_order[element]
            #id = self.ir.finite_elements.index(element)

            cshape_str = "shape_" + str(id)
            decl += "int " + cshape_str + "[4];\n"
            decl += "basix_element_tabulate_shape("
            decl += "elements[" + str(id) + "], "
            decl += str(self.backend.symbols.custom_num_points()) + ", "
            decl += str(nd) + ", "
            decl += cshape_str + ");\n"

            # int table_size = shape[0]*shape[1]*shape[2]*shape[3];
            # double table[shape[0]][shape[1]][shape[2]][shape[3]];
            # basix_element_tabulate(element, points, num_points, nd, (double*) table, table_size);
            decl += "int table_size_" + str(id) + "="
            decl += cshape_str + "[0]*" + cshape_str + "[1]*" + cshape_str + "[2]*" + cshape_str + "[3];\n"
            decl += "double table_" + str(id) + "[" + cshape_str + "[0]][" + cshape_str + "[1]]["
            decl += cshape_str + "[2]][" + cshape_str + "[3]];\n"

            decl += "basix_element_tabulate("
            decl += "elements[" + str(id) + "], "
            decl += "gdim, points, "
            decl += str(self.backend.symbols.custom_num_points()) + ", "
            decl += str(nd) + ", "
            decl += "(double *) table_"+ str(id) + ", table_size_" + str(id) + ");\n"

        for i in range(0,len(element_tables)):
          decl += "basix_element_destroy(elements[" + str(i) + "]);\n"

        tabulate_parts += [L.LiteralString(decl)] #[L.StatementList(decl)] #[L.VerbatimStatement(decl)]

        #tabulate_parts = L.commented_code_list(tabulate_parts,
        #                                       ["Tabulate basis functions and their derivatives",
        #                                        "dim: [derivatives (basix::idx)][point][basis fn][function component]"])

        table_parts = []
        for t_id, e in element_tables.items():
            table = tables[e.name]
            id = self.ir.finite_elements.index(e.component_element_name)

            # Replace number of points in array with symbolic number of points that is passed to
            # tabulate tensor at run-time
            brackets = ''.join("[%d]" % table.shape[0])
            brackets += ''.join("[%d]" % table.shape[1])
            brackets += ''.join(f"[{self.backend.symbols.custom_num_points()!s}]")
            brackets += ''.join("[%d]" % table.shape[3])

            # Declare array of the type e.g. double FE#_C#[1][1][num_points][3];
            # to be filled at run-time using basix tabulate call
            decl = "double " + " " + e.name + brackets + ";\n"

            table_parts += [L.LiteralString(decl)]

            num_points = self.backend.symbols.custom_num_points()
            iq = self.backend.symbols.quadrature_loop_index
            arg_indices = tuple(self.backend.symbols.argument_loop_index(i) for i in range(3))

            brackets = []
            if (table.shape[0] > 1):
                brackets = ''.join(f"[{arg_indices[0].name}]")
            else:
                brackets = ''.join("[0]")

            if (table.shape[1] > 1):
                brackets += ''.join(f"[{arg_indices[1].name}]")
            else:
                brackets += ''.join("[0]")

            brackets += ''.join(f"[{iq.name}]")
            brackets += ''.join(f"[{arg_indices[2].name}]")
            body = e.name + brackets + " = "

            # Element tables gives metadata associated to name: (id, basix_index,fc)
            # table dimensions: [derivatives (basix::indexing)][point index][basis function index][function component]
            brackets = ''.join("[%d]" % e.basix_index)
            brackets += ''.join(f"[{iq.name}]")
            brackets += ''.join(f"[{arg_indices[2].name}]")
            brackets += ''.join("[%d]" % e.fc)
            body += "table_" + str(id) + brackets + ";"

            body_L = [L.LiteralString(body)]

            # #body = L.create_nested_for_loops([iq], body)

            body = [L.ForRange(arg_indices[2], 0, table.shape[3], body=body_L)]
            body = [L.ForRange(iq, 0, num_points, body=body)]
            if (table.shape[1] > 1):
                body = [L.ForRange(arg_indices[1], 0, table.shape[1], body=body)]
            if (table.shape[0] > 1):
                body = [L.ForRange(arg_indices[0], 0, table.shape[0], body=body)]

            table_parts += [body]

        # Add leading comment if there are any tables
        table_parts = L.commented_code_list(table_parts, [
            "Array for basis evaluations and basis derivative evaluations",
            "FE* dimensions: [permutation][entities][points][dofs]"])

        return element_def_parts, tabulate_parts, table_parts
