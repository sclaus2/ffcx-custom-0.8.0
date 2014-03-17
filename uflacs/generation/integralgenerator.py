
from ufl.common import product

from uflacs.utils.log import debug, info, warning, error, uflacs_assert

from uflacs.analysis.modified_terminals import analyse_modified_terminal2, is_modified_terminal

from uflacs.codeutils.format_code import (format_code, Indented, Block, Comment,
                                          ForRange,
                                          ArrayDecl, ArrayAccess,
                                          Assign, AssignAdd,
                                          Product)
from uflacs.codeutils.indexmapping import IndexMapping, AxisMapping
from uflacs.geometry.default_names import names
from uflacs.codeutils.expr_formatter2 import ExprFormatter2


class IntegralGenerator(object):
    def __init__(self, ir, language_formatter, backend_access, backend_definitions):
        # Store ir
        self.ir = ir

        # Consistency check on quadrature rules
        nps1 = sorted(ir["uflacs"]["expr_ir"].keys())
        nps2 = sorted(ir["quadrature_rules"].keys())
        if nps1 != nps2:
            uflacs_warning("Got different num_points for expression irs and quadrature rules:\n{0}\n{1}".format(
                nps1, nps2))

        # Compute shape of element tensor
        if self.ir["domain_type"] == "interior_facet":
            self._A_shape = [2*n for n in self.ir["prim_idims"]]
        else:
            self._A_shape = self.ir["prim_idims"]

        # TODO: Populate these
        self._using_names = set()
        self._includes = set(("#include <cstring>",
                              "#include <cmath>"))

        self.backend_access = backend_access
        self.backend_definitions = backend_definitions

        # This is a transformer that collects terminal modifiers
        # and delegates formatting to the language_formatter
        self.expr_formatter = ExprFormatter2(language_formatter, {})

    def generate_using_statements(self):
        return ["using %s;" % name for name in sorted(self._using_names)]

    def get_includes(self):
        return sorted(self._includes)

    def generate(self):
        """Generate entire tabulate_tensor body.

        Assumes that the code returned from here will be wrapped in a context
        that matches a suitable version of the UFC tabulate_tensor signatures.
        """
        parts = []
        parts += [self.generate_using_statements()]
        parts += [self.backend_definitions.initial()]
        parts += [self.generate_quadrature_tables()]
        parts += [self.generate_element_tables()]
        parts += [self.generate_tensor_reset()]
        parts += [self.generate_piecewise_partition()]
        parts += [self.generate_quadrature_loops()]
        parts += [self.generate_finishing_statements()]
        return format_code(Indented(parts))

    def generate_quadrature_tables(self):
        "Generate static tables of quadrature points and weights."
        parts = []

        qrs = self.ir["quadrature_rules"]
        if qrs:
            parts += ["// Section for quadrature weights and points"]

        for num_points in sorted(qrs):
            weights = qrs[num_points][0]
            points = qrs[num_points][1]

            # Size of quadrature points depends on context, assume this is correct:
            pdim = len(points[0])

            wname = "%s%d" % (names.weights, num_points)
            pname = "%s%d" % (names.points, num_points)

            weights = [self.backend_access.precision_float(w) for w in weights]
            points = [self.backend_access.precision_float(x) for p in points for x in p]

            parts += [ArrayDecl("static const double", wname, num_points, weights)]
            parts += [ArrayDecl("static const double", pname, num_points*pdim, points)]
            parts += [""]

        return parts

    def generate_element_tables(self):
        "Generate static tables with precomputed element basis function values in quadrature points."
        parts = []
        parts += [Comment("Section for precomputed element basis function values")]
        expr_irs = self.ir["uflacs"]["expr_ir"]
        for num_points in sorted(expr_irs):
            tables = expr_irs[num_points]["unique_tables"]
            comment = "Definitions of {0} tables for {0} quadrature points".format(len(tables), num_points)
            parts += [Comment(comment)]
            for name in sorted(tables):
                table = tables[name]
                if product(table.shape) > 0:
                    parts += [ArrayDecl("static const double", name, table.shape, table)]
        return parts

    def generate_tensor_reset(self):
        "Generate statements for resetting the element tensor to zero."

        # Compute tensor size
        A_size = product(self._A_shape)

        # TODO: Make this an ast primitive?
        def memzero(ptrname, size):
            return "memset({ptrname}, 0, {size} * sizeof(*{ptrname}));".format(ptrname=ptrname, size=size)

        parts = []
        parts += [Comment("Reset element tensor")]
        parts += [memzero(names.A, A_size)]
        parts += [""]
        return parts

    def generate_quadrature_loops(self):
        "Generate all quadrature loops."
        parts = []
        for num_points in sorted(self.ir["uflacs"]["expr_ir"]):
            body = self.generate_quadrature_body(num_points)
            parts += [ForRange(names.iq, 0, num_points, body=body)]
        return parts

    def generate_quadrature_body(self, num_points):
        """
        """
        parts = []
        parts += ["// Quadrature loop body setup {0}".format(num_points)]
        parts += self.generate_varying_partition(num_points)

        # Compute single argument partitions outside of the dofblock loops
        for iarg in range(self.ir["rank"]):
            for dofrange in []: # TODO: Move f*arg0 out here
                parts += self.generate_argument_partition(num_points, iarg, dofrange)

        # Nested argument loops and accumulation into element tensor
        parts += self.generate_quadrature_body_dofblocks(num_points)

        return parts

    def generate_quadrature_body_dofblocks(self, num_points, outer_dofblock=()):
        parts = []

        # The loop level iarg here equals the argument count (in renumbered >= 0 format)
        iarg = len(outer_dofblock)
        if iarg == self.ir["rank"]:
            # At the innermost argument loop level we accumulate into the element tensor
            parts += [self.generate_integrand_accumulation(num_points, outer_dofblock)]
            return parts
        assert iarg < self.ir["rank"]

        expr_ir = self.ir["uflacs"]["expr_ir"][num_points]
        # tuple(modified_argument_indices) -> code_index
        AF = expr_ir["argument_factorization"]

        # modified_argument_index -> (tablename, dofbegin, dofend)
        MATR = expr_ir["modified_argument_table_ranges"]

        # Find dofranges at this loop level iarg starting with outer_dofblock
        dofranges = sorted(set(MATR[mas[iarg]][1:3] for mas in AF
                           if all(MATR[mas[i]][1:3] == j for i,j in enumerate(outer_dofblock))
                           ))

        # Build loops for each dofrange
        for dofrange in dofranges:
            dofblock = outer_dofblock + (dofrange,)
            body = []

            # Generate nested inner loops (only triggers for forms with two or more arguments
            body += self.generate_quadrature_body_dofblocks(num_points, dofblock)

            # Wrap setup, subloops, and accumulation in a loop for this level
            idof = "{name}{num}".format(name=names.ia, num=iarg)
            parts += [ForRange(idof, dofrange[0], dofrange[1], body=body)]
        return parts

    def generate_partition(self, name, V, partition, table_ranges): # TODO: Rather take list of vertices, not markers
        terminalcode = []
        assignments = []
        from ufl.classes import ConstantValue
        j = 0
        #print "Generating partition ", name
        for i,p in enumerate(partition):
            if p:
                # TODO: Consider optimized ir here with markers for which subexpressions to store in variables.
                # This code just generates variables for _all_ subexpressions marked by p.

                v = V[i]

                if is_modified_terminal(v):
                    mt = analyse_modified_terminal2(v)
                    if isinstance(mt.terminal, ConstantValue):
                        vaccess = self.expr_formatter.visit(v)
                    else:
                        vaccess = self.backend_access(mt.terminal, mt, table_ranges[i])
                        vdef = self.backend_definitions(mt.terminal, mt, table_ranges[i], vaccess)
                        terminalcode += [vdef]
                else:
                    # Count assignments so we get a new vname each time
                    vaccess = ArrayAccess(name, j)
                    j += 1
                    vcode = self.expr_formatter.visit(v) # TODO: Generate Code instead of str here?
                    assignments += [Assign(vaccess, vcode)]

                vname = format_code(vaccess) # TODO: Can skip this if expr_formatter generates Code
                #print '\nStoring {0} = {1}'.format(vname, str(v))
                self.expr_formatter.variables[v] = vname

        parts = []
        if j > 0:
            # Compute all terminals first
            parts += terminalcode
            # Declare array large enough to hold all subexpressions we've emitted
            parts += [ArrayDecl("double", name, j)]
            # Then add all computations
            parts += assignments
        return parts

    def generate_piecewise_partition(self):
        """Generate statements prior to the quadrature loop.

        This mostly includes computations involving piecewise constant geometry and coefficients.
        """
        parts = []
        parts += ["// Section for piecewise constant computations"]
        expr_irs = self.ir["uflacs"]["expr_ir"]
        for num_points in sorted(expr_irs):
            expr_ir = expr_irs[num_points]
            parts += self.generate_partition("sp",
                                             expr_ir["V"],
                                             expr_ir["piecewise"],
                                             expr_ir["table_ranges"])
        return parts

    def generate_varying_partition(self, num_points):
        parts = []
        parts += ["// Section for geometrically varying computations"]
        expr_ir = self.ir["uflacs"]["expr_ir"][num_points]
        parts += self.generate_partition("sv",
                                         expr_ir["V"],
                                         expr_ir["varying"],
                                         expr_ir["table_ranges"])
        return parts

    def generate_argument_partition(self, num_points, iarg, dofrange):
        """Generate code for the partition corresponding to arguments 0..iarg within given dofblock."""
        parts = []
        # TODO: What do we want to do here? Define!
        # Should this be a single loop over i0, i1 separately outside of the double loop over (i0,i1)?
        return parts

    def generate_integrand_accumulation(self, num_points, dofblock):
        parts = []

        expr_ir = self.ir["uflacs"]["expr_ir"][num_points]
        AF = expr_ir["argument_factorization"]
        V = expr_ir["V"]
        MATR = expr_ir["modified_argument_table_ranges"]
        MA = expr_ir["modified_arguments"]

        idofs = ["{0}{1}".format(names.ia, i) for i in range(self.ir["rank"])]

        # Find the blocks to build: (TODO: This is rather awkward, having to rediscover these relations here)
        arguments_and_factors = sorted(expr_ir["argument_factorization"].items(), key=lambda x: x[0])
        for args, factor_index in arguments_and_factors:
            if not all(tuple(dofblock[iarg]) == tuple(MATR[ma][1:3]) for iarg, ma in enumerate(args)):
                continue

            factors = []

            # Get factor expression # TODO: Omit f if it's 1 or 1.0
            f = V[factor_index]
            fcode = self.expr_formatter.visit(f)
            factors += [fcode]

            # Get table names
            argfactors = []
            for i, ma in enumerate(args):
                mt = analyse_modified_terminal2(MA[ma])
                access = self.backend_access(mt.terminal, mt, MATR[ma])
                argfactors += [access]
            factors.extend(argfactors)

            # Format index access to A
            if idofs:
                dofdims = zip(idofs, self._A_shape)
                im = IndexMapping(dict((idof, idim) for idof, idim in dofdims))
                am = AxisMapping(im, [idofs])
                A_ii, = am.format_index_expressions()
            else:
                A_ii = 0
            A_access = ArrayAccess(names.A, A_ii)

            # Emit assignment
            parts += [AssignAdd(A_access, Product(factors))]

        return parts

    def generate_finishing_statements(self):
        """Generate finishing statements.

        This includes assigning to output array if there is no integration.
        """
        parts = []

        if not self.ir["quadrature_rules"]: # Rather check ir["domain_type"]?
            # TODO: Implement for expression support
            error("Expression generation not implemented yet.")
            # TODO: If no integration, assuming we generate an expression, and assign results here
            # Corresponding code from compiler.py:
            #assign_to_variables = tfmt.output_variable_names(len(final_variable_names))
            #parts += list(format_assignments(zip(assign_to_variables, final_variable_names)))

        return parts


    def dummy(self):
        expr_ir = self.ir["uflacs"]["expr_ir"][num_points]

        # Core expression graph:
        expr_ir["V"]
        expr_ir["target_variables"]

        # Result of factorization:
        expr_ir["modified_arguments"]
        expr_ir["argument_factorization"]

        # Metadata and dependency information:
        expr_ir["active"]
        expr_ir["varying"]
        expr_ir["piecewise"]
        expr_ir["dependencies"]
        expr_ir["inverse_dependencies"]
        expr_ir["modified_terminal_indices"]

        # Table names and dofranges
        expr_ir["modified_argument_table_ranges"]
        expr_ir["modified_terminal_table_ranges"]

        # Generate pre-argument loop computations # TODO: What is this?
        expr_ir = self.ir["uflacs"]["expr_ir"][num_points]
        # tuple(modified_argument_indices) -> code_index
        AF = expr_ir["argument_factorization"]
        # modified_argument_index -> (tablename, dofbegin, dofend)
        MATR = expr_ir["modified_argument_table_ranges"]
        for mas in sorted(AF):
            dofblock = tuple(MATR[ma][1:3] for ma in mas)
            ssa_index = AF[mas]
            # TODO: Generate code for f*D and store reference to it with mas
            #f = self._ssa[ssa_index]
            #vname = name_from(mas)
            #vcode = code_from(ssa_index)
            #code += ["%s = (%s) * D;" % (vname, vcode)]
            #factors[mas] = vname

    def foobar(self):
        # In code generation, do something like:
        matr = expr_ir["modified_argument_table_ranges"]
        for mas, factor in expr_ir["argument_factorization"].iteritems():
            fetables = tuple(matr[ma][0] for ma in mas)
            dofblock = tuple((matr[ma][1:3]) for ma in mas)
            modified_argument_blocks[dofblock] = (fetables, factor)
            #
            #for (i0=dofblock0[0]; i0<dofblock0[1]; ++i0)
            #  for (i1=dofblock1[0]; i1<dofblock1[1]; ++i1)
            #    A[i0*n1 + i1] += (fetables[0][iq][i0-dofblock0[0]]
            #                    * fetables[1][iq][i1-dofblock1[0]]) * V[factor] * weight;
            #




class CellIntegralGenerator(IntegralGenerator):
    pass

class ExteriorFacetIntegralGenerator(IntegralGenerator):
    pass

class InteriorFacetIntegralGenerator(IntegralGenerator):
    pass

class DiracIntegralGenerator(IntegralGenerator):
    pass

class QuadratureIntegralGenerator(IntegralGenerator):
    pass
