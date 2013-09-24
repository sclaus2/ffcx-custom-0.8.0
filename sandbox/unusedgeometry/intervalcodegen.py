from uflacs.geometry.cellcodegen import CellGeometryCG

# FIXME: Test all these in generated tabulate tensor context

class IntervalGeometryCG(CellGeometryCG):
    def __init__(self, restriction=''):
        CellGeometryCG.__init__(self, 'interval', 1, 1, restriction)

    def J_code(self):
        return 'const double %(J)s[%(gdim)s*%(gdim)s] = { %(coords)s[1*%(gdim)d + 0] - %(coords)s[0*%(gdim)d + 0] };' % self.vars

    def detJ_code(self):
        return 'const double %(detJ)s = %(J)s[0];' % self.vars

    def Jinv_code(self):
        return 'const double %(Jinv)s[1] = { 1.0 / %(J)s[0] };' % self.vars

    def x_from_xi_code(self):
        return 'double %(x)s[1] = { %(J)s[0]*%(xi)s[0] + %(v0)s[0] };' % self.vars

    def xi_from_x_code(self):
        return 'double %(xi)s[1] = { %(Jinv)s[0]*(%(x)s[0] - %(v0)s[0]) };' % self.vars

    def cell_volume_code(self):
        return 'double %(volume)s = fabs(%(detJ)s);' % self.vars

    def circumradius_code(self):
        return 'double %(circumradius)s = fabs(%(detJ)s);' % self.vars # FIXME

    def facet_direction_code(self):
        return 'bool %(facet_direction)s = %(coords)s[0*%(gdim)d + 0] < %(coords)s[1*%(gdim)d + 0];' % self.vars

    def facet_area_code(self):
        return 'double %(facet_area)s = 1.0;' % self.vars

    def facet_normal_code(self):
        return 'double %(facet_normal)s[%(gdim)s] = { (%(facet)s == 0 ? -1.0: 1.0) * (%(facet_direction)s ? 1.0: -1.0) };' % self.vars
