
class CellGeometryNames(object):
    def __init__(self, restriction=''):
        # Names required for determining other names below
        self.vars = { 'restriction': restriction }

        # Fairly generic names
        names = {
            'v0':       'v0%(restriction)s' % self.vars,
            'J':        'J%(restriction)s' % self.vars,
            'Jinv':     'Jinv%(restriction)s' % self.vars,
            'detJ':     'detJ%(restriction)s' % self.vars,
            'absdetJ':  'absdetJ%(restriction)s' % self.vars,
            'signdetJ': 'signdetJ%(restriction)s' % self.vars,
            'x':        'x%(restriction)s' % self.vars,
            'xi':       'xi%(restriction)s' % self.vars,
            'volume':             'volume%(restriction)s' % self.vars,
            'surface':            'surface%(restriction)s' % self.vars,
            'circumradius':       'circumradius%(restriction)s' % self.vars,
            'facet_determinant':  'facet_determinant',
            'facet_area':         'facet_area',
            'facet_normal':       'n%(restriction)s' % self.vars,
            }

        # UFC specific names!
        ufc_names = {
            'c':        'c%(restriction)s' % self.vars,
            'coords':   'vertex_coordinates%(restriction)s' % self.vars,
            }

        self.vars.update(names)
        self.vars.update(ufc_names)

    # ... Accessors for names of geometry quantites:

    def x(self):
        return self.vars['x']

    def xi(self):
        return self.vars['xi']

    def J(self):
        return self.vars['J']

    def Jinv(self):
        return self.vars['Jinv']

    def detJ(self):
        return self.vars['detJ']

    def cell_volume(self):
        return self.vars['volume']

    def circumradius(self):
        return self.vars['circumradius']

    def cell_surface_area(self):
        return self.vars['surface']

    def facet_normal(self):
        return self.vars['facet_normal']

    def facet_area(self):
        return self.vars['facet_area']


class CellGeometryCG(CellGeometryNames):
    """Code generation of cell related geometry snippets.

    x[]: global coordinates
    xi[]: local cell coordinates
    J[i*d+j]: d xi[i] / d x[j]
    x[i] = sum_j J[i*d+j]*xi[j] + v0[i]
    xi[i] = sum_j Jinv[i*d+j]*(x[j] - v0[i])
    """
    def __init__(self, celltype, gdim, tdim, restriction=''):
        CellGeometryNames.__init__(self, restriction)
        self.celltype = celltype
        self.gdim = gdim
        self.tdim = tdim
        self.vars.update({
            'celltype': celltype,
            'gdim': gdim,
            'tdim': tdim,
            })

    # ... Code snippets for declaring and computing the above quantities:

    def v0_code(self):
        "UFC specific!"
        return "const double * %(v0)s = %(coords)s;" % self.vars

    def J_code(self):
        raise NotImplementedException

    def detJ_code(self):
        raise NotImplementedException

    def signdetJ_code(self):
        return "const double %(signdetJ)s = %(detJ)s >= 0.0 ? +1.0: -1.0;" % self.vars

    def absdetJ_code(self):
        return "const double %(absdetJ)s = %(detJ)s * %(signdetJ)s;" % self.vars

    def Jinv_code(self):
        raise NotImplementedException

    def x_from_xi_code(self):
        raise NotImplementedException

    def xi_from_x_code(self):
        raise NotImplementedException

    def cell_volume_code(self):
        raise NotImplementedException

    def circumradius_code(self):
        raise NotImplementedException

    def cell_surface_area_code(self):
        raise NotImplementedException

    def facet_normal_code(self):
        raise NotImplementedException

    def facet_area_code(self):
        raise NotImplementedException
