"""
This module implements the formatting of UFC code from a given
dictionary of generated C++ code for the body of each UFC function.

It relies on templates for UFC code available as part of the module
ufc_utils. It also relies on templates for generation of specialized
DOLFIN additions available as part of the module dolfin_utils.
"""

__author__ = "Anders Logg (logg@simula.no) and friends"
__date__ = "2009-12-16"
__copyright__ = "Copyright (C) 2009 " + __author__
__license__  = "GNU GPL version 3 or any later version"

# Last changed: 2009-12-17

# Python modules
import os

# UFC finite_element templates
from ufc_utils import finite_element_combined
from ufc_utils import finite_element_header
from ufc_utils import finite_element_implementation

# UFC dof_map templates
from ufc_utils import dof_map_combined
from ufc_utils import dof_map_header
from ufc_utils import dof_map_implementation

# FFC modules
from ffc.log import info

def format_ufc(codes, prefix, options):
    "Format given code in UFC format."

    # Generate code for header
    #output = _generate_header(prefix, options)
    #if self.output_format == "ufc":
    #    output += _generate_header(prefix, options)
    #elif self.output_format == "dolfin":
    #    output += _generate_dolfin_header(prefix, options)
    #    output += "\n"

    # Iterate over codes
    output = ""
    for (i, code) in enumerate(codes):

        # Extract generated code
        code_forms, code_elements, code_dofmaps = code

        # Generate code for elements
        for code_element in code_elements:
            output += finite_element_combined % code_element

        # Generate code for dofmaps
        for code_dofmap in code_dofmaps:
            output += dof_map_combined % code_dofmap

    # Write generated code to file
    prefix = prefix.split(os.path.join(' ',' ').split()[0])[-1]
    full_prefix = os.path.join(options["output_dir"], prefix)
    filename = "%s.h" % full_prefix
    file = open(filename, "w")
    file.write(output)
    file.close()
    info("Output written to " + filename + ".")
