from __future__ import print_function
# from __future__ import division
# from readgri import readgri

import numpy as np
import matplotlib.pyplot as plt
from meshes import Mesh
# import flux_lib
import time
# from solver import fvsolver as solver
# from solver import mesh as solver_mesh
# from solver import constants as solver_constants
# from solver import postprocess as solver_post

from dg_solver import fluxes

import dg_solver
import quadrules
# import solver

# TODO
"""

    # do post processing for outputs

    #

"""


class DGSolver(object):
    def __init__(self, mesh, order=1):
        """
            class to solver for the flow in a given mesh
        """

        # self.mesh = mesh
        # self.U = np.zeros((mesh.nElem, 4))
        self.order = order
        self.mesh = mesh
        self.mesh.curvOrder = order+1
        self.nStates = 4

        # set BC data
        self.gamma = 1.4
        self.mach_Inf = 0.5
        self.R = 1.0
        self.rho_Inf = 1.
        self.P_inf = 1.
        self.temp_Inf = 1.
        self.alpha = 0.0

        self.tempTot_inf = 1 + (self.gamma - 1)/2 * self.mach_Inf**2*self.temp_Inf
        self.Ptot_inf = self.tempTot_inf**(self.gamma/(self.gamma - 1))

        # self.Tt = (1 + 0.5*(self.gamma - 1)*self.mach_Inf**2)*self.temp_Inf

        # set mesh data

        # this method assumes that there is only one of each bc type

        self.curvWall = -1
        self.wall = -1
        self.inlet = -1
        self.outlet = -1
        for idx, bcname in enumerate(mesh.BCs.keys()):
            if 'curv' in bcname and 'wall' in bcname:
                self.curvWall = idx
            elif 'wall' in bcname:
                self.wall = idx
            elif 'inlet' in bcname:
                self.inlet = idx
            elif 'outlet' in bcname:
                self.outlet = idx
        # ====================================================
        #  Basis
        # ====================================================
        # get the basis function that are used to represent the solution
        self.nSolBasis, self.solBasis = quadrules.getTriLagrangeBasis2D(order)

        # number of degrees of freedom
        self.dof = self.mesh.nElem*self.nSolBasis

        # basis fucntion to represent the curved geometric elements
        _, self.curvBasis = quadrules.getTriLagrangeBasis2D(self.mesh.curvOrder)

        self.initFreestream()

        # ====================================================
        #  Qaud rules for the mesh (get seperate rules for the linear and curved elements)
        # ====================================================

        self.mesh.getHighOrderNodes()

        # for maximum python efficency these two nearly identical blocks should be a loop
        # but it works better with fortran this way

        # --------------------- Linear Element Quadrature ------------------------

        # these are 1d points form 0 to 1 (used for edge integration)
        self.linQuadPts1D, self.linQuadWts1D = quadrules.getQuadPts1D(order+1, 0, 1)
        self.nLinQuadPts1D = len(self.linQuadWts1D)

        # these are 2d points of a tri element in reference space
        self.linQuadPts2D, self.linQuadWts2D = quadrules.getQuadPtsTri(order+2)
        self.nLinQuadPts2D = len(self.linQuadWts2D)

        self.linPhi, self.linGPhi = self.getPhiMat(self.linQuadPts2D)
        self.lLinEdgePhi, self.rLinEdgePhi = self.getEdgePhi(self.linQuadPts1D)

        # get jacobian

        # ** note this is done for **all** elements because the edge residuals for the stright edges of the
        # curved element can be calculated using a linear jacobian
        #       - this isn't true and should be fixed
        self.linInvJ, self.linDetJ = self.mesh.getLinearJacobian()

        # calculate inverse mass matrix for each element
        self.invM = np.zeros((self.mesh.nElem, self.nSolBasis, self.nSolBasis))
        for k, idx_elem in enumerate(self.mesh.linElem):
            M = self.linDetJ[k] * np.matmul(np.matmul(self.linPhi.T,
                                                      np.diag(self.linQuadWts2D)), self.linPhi)
            self.invM[idx_elem] = np.linalg.inv(M)
        # --------------------- Curved Element Quadrature -------------------------

        # these are 1d points form 0 to 1 (used for edge integration)
        self.curvQuadPts1D, self.curvQuadWts1D = quadrules.getQuadPts1D(self.mesh.curvOrder+1, 0, 1)
        self.nCurvQuadPts1D = len(self.curvQuadWts1D)

        # these are 2d points of a tri element in reference space
        self.curvQuadPts2D, self.curvQuadWts2D = quadrules.getQuadPtsTri(self.mesh.curvOrder+2)
        self.nCurvQuadPts2D = len(self.curvQuadWts2D)

        self.curvPhi, self.curvGPhi = self.getPhiMat(self.curvQuadPts2D)
        self.lCurvEdgePhi, self.rCurvEdgePhi = self.getEdgePhi(self.curvQuadPts1D)

        # get jacobian
        self.mesh.nCurvElem = len(self.mesh.curvElem)
        # self.curvJ = np.zeros((self.mesh.nCurvElem, self.nCurvQuadPts2D, 2, 2))
        self.curvInvJ = np.zeros((self.mesh.nCurvElem, self.nCurvQuadPts2D, 2, 2))
        self.curvDetJ = np.zeros((self.mesh.nCurvElem, self.nCurvQuadPts2D))

        for idx_elem, _ in enumerate(self.mesh.curvElem):
            # print elem
            _, self.curvInvJ[idx_elem], self.curvDetJ[idx_elem] = self.mesh.getCurvedJacobian(
                idx_elem, self.curvQuadPts2D, self.curvBasis)
        # calculate inverse mass matrix for each element
        for idx_elem, k in enumerate(self.mesh.curvElem):
            M = np.matmul(np.matmul(self.curvPhi.T, np.diag(
                self.curvDetJ[idx_elem]*self.curvQuadWts2D)), self.curvPhi)
            self.invM[k] = np.linalg.inv(M)

        self.curvDetJEdge, self.curvNormal = self.mesh.getEdgeJacobain(self.curvQuadPts1D, self.curvBasis)

        # TODO
        # tranfer precomputed values to the fortran layer
        self.setFortranVariables()

    def setFortranVariables(self):
        """
            transfers all of the precomputed values to the fortran layer so they can be used for solving

            +1 and reshaping everwhere becuase fortran is index 1 and column major
        """
        # set external flow constants
        dg_solver.constants.temptot_inf = self.tempTot_inf
        dg_solver.constants.ptot_inf = self.Ptot_inf
        dg_solver.constants.p_inf = self.P_inf
        dg_solver.constants.alpha = self.alpha

        # quadrature parameters
        dg_solver.quadrature.nlinquadpts1d = self.nLinQuadPts1D
        dg_solver.quadrature.nlinquadpts2d = self.nLinQuadPts2D
        dg_solver.quadrature.linquadwts1d = self.linQuadWts1D
        dg_solver.quadrature.linquadwts2d = self.linQuadWts2D

        dg_solver.quadrature.ncurvquadpts1d = self.nCurvQuadPts1D
        dg_solver.quadrature.ncurvquadpts2d = self.nCurvQuadPts2D
        dg_solver.quadrature.curvquadwts1d = self.curvQuadWts1D
        dg_solver.quadrature.curvquadwts2d = self.curvQuadWts2D

        # set mesh properties
        dg_solver.mesh.curvwall = self.curvWall + 1
        dg_solver.mesh.wall = self.wall + 1
        dg_solver.mesh.inlet = self.inlet + 1
        dg_solver.mesh.outlet = self.outlet + 1

        dg_solver.mesh.innormal = self.mesh.inNormal.T
        dg_solver.mesh.bcnormal = self.mesh.bcNormal.T
        dg_solver.mesh.inlength = self.mesh.inLength
        dg_solver.mesh.bclength = self.mesh.bcLength
        dg_solver.mesh.inedge2elem = self.mesh.inEdge2Elem.T + 1
        dg_solver.mesh.bcedge2elem = self.mesh.bcEdge2Elem.T + 1
        dg_solver.mesh.area = self.mesh.area
        dg_solver.mesh.nelem = self.mesh.nElem
        dg_solver.mesh.ninedge = self.mesh.nInEdge
        dg_solver.mesh.nbcedge = self.mesh.nBCEdge

        dg_solver.mesh.linelem = self.mesh.linElem + 1
        dg_solver.mesh.nlinelem = self.mesh.nLinElem
        dg_solver.mesh.curvelem = self.mesh.curvElem + 1
        dg_solver.mesh.ncurvelem = self.mesh.nCurvElem

        dg_solver.mesh.elem2curvelem = self.mesh.elem2CurvElem + 1

        dg_solver.mesh.invm = self.invM.reshape(self.nSolBasis, self.nSolBasis, self.mesh.nElem)
        for idx_elem in range(self.mesh.nElem):
            dg_solver.mesh.invm[:, :, idx_elem] = self.invM[idx_elem]

        dg_solver.mesh.lininvj = np.asfortranarray(self.linInvJ.T)
        for idx_linElem in range(self.mesh.nLinElem):
            dg_solver.mesh.lininvj[:, :, idx_linElem] = self.linInvJ[idx_linElem]

        dg_solver.mesh.lindetj = self.linDetJ

       # check these np.zeros((self.mesh.nCurvElem, self.nCurvQuadPts2D, 2, 2))
        dg_solver.mesh.curvinvj = self.curvInvJ.reshape(2, 2, self.nCurvQuadPts2D, self.mesh.nCurvElem)
        for idx_curvElem in range(self.mesh.nCurvElem):
            for q in range(self.nCurvQuadPts2D):
                dg_solver.mesh.curvinvj[:, :, q, idx_curvElem] = self.curvInvJ[idx_curvElem, q]

        dg_solver.mesh.curvdetj = self.curvDetJ.T
        # nElem,  3, nQuadPts1D, 2

        # curvDetJEdge

        dg_solver.mesh.curvdetjedge = self.curvDetJEdge.reshape(self.nCurvQuadPts1D, 3, self.mesh.nCurvElem)
        dg_solver.mesh.curvnormal = self.curvNormal.reshape(2, self.nCurvQuadPts1D, 3, self.mesh.nCurvElem)
        for idx_curvElem in range(self.mesh.nCurvElem):
            for idx_edge in range(3):
                for q in range(self.nCurvQuadPts1D):
                    dg_solver.mesh.curvnormal[:, q, idx_edge, idx_curvElem] = self.curvNormal[idx_curvElem, idx_edge, q]
                    dg_solver.mesh.curvdetjedge[q, idx_edge,
                                                idx_curvElem] = self.curvDetJEdge[idx_curvElem, idx_edge, q]
        # print(self.curvDetJEdge)

        # ------------- basis function values ----------
        dg_solver.basis.nsolbasis = self.nSolBasis

        dg_solver.basis.linphi = self.linPhi.T

        dg_solver.basis.lingphi = self.linGPhi.reshape(self.mesh.nDim, self.nSolBasis, self.nLinQuadPts2D)
        for q in range(self.nLinQuadPts2D):
            for idx_sol in range(self.nSolBasis):
                dg_solver.basis.lingphi[:, idx_sol, q] = self.linGPhi[q, idx_sol, :]

        # print(self.rLinEdgePhi)
        dg_solver.basis.llinedgephi = self.lLinEdgePhi.reshape(self.nSolBasis, self.nLinQuadPts1D, 3)
        dg_solver.basis.rlinedgephi = self.rLinEdgePhi.reshape(self.nSolBasis, self.nLinQuadPts1D, 3)
        for idx_edge in range(3):
            for q in range(self.nLinQuadPts1D):
                for idx_sol in range(self.nSolBasis):
                    dg_solver.basis.llinedgephi[idx_sol, q, idx_edge] = self.lLinEdgePhi[idx_edge, q, idx_sol]
                    dg_solver.basis.rlinedgephi[idx_sol, q, idx_edge] = self.rLinEdgePhi[idx_edge, q, idx_sol]

        dg_solver.basis.curvphi = self.curvPhi.T

        dg_solver.basis.curvgphi = self.curvGPhi.reshape(self.mesh.nDim, self.nSolBasis, self.nCurvQuadPts2D)
        for q in range(self.nCurvQuadPts2D):
            for idx_sol in range(self.nSolBasis):
                dg_solver.basis.curvgphi[:, idx_sol, q] = self.curvGPhi[q, idx_sol, :]
                # print(dg_solver.basis.curvgphi[:, idx_sol, q])

        dg_solver.basis.lcurvedgephi = self.lCurvEdgePhi.reshape(self.nSolBasis, self.nCurvQuadPts1D, 3)
        dg_solver.basis.rcurvedgephi = self.rCurvEdgePhi.reshape(self.nSolBasis, self.nCurvQuadPts1D, 3)
        for idx_edge in range(3):
            for q in range(self.nCurvQuadPts1D):
                for idx_sol in range(self.nSolBasis):
                    dg_solver.basis.lcurvedgephi[idx_sol, q,  idx_edge] = self.lCurvEdgePhi[idx_edge, q, idx_sol]
                    dg_solver.basis.rcurvedgephi[idx_sol, q, idx_edge] = self.rCurvEdgePhi[idx_edge, q, idx_sol]

    def getPhiMat(self, quadPts2D):
        nQuadPts2D = len(quadPts2D)

        Phi = np.zeros((nQuadPts2D, self.nSolBasis))
        dPhi_dXi = np.zeros((nQuadPts2D, self.nSolBasis, self.mesh.nDim))

        # import ipdb
        # ipdb.set_trace()
        for idx, pt in enumerate(quadPts2D):
            # the basis function value at each of the quad points
            Phi[idx], dPhi_dXi[idx] = self.solBasis(pt)

        return Phi, dPhi_dXi

    # precompute the values of the basis functions are each edge of the reference element
    def getEdgePhi(self, quadPts1D):
        # 3 because there are three faces of a triangle
        nQuadPts1D = len(quadPts1D)

        leftEdgePhi = np.zeros((3, nQuadPts1D, self.nSolBasis))
        rightEdgePhi = np.zeros((3, nQuadPts1D, self.nSolBasis))
        for edge in range(3):
            # map 2D fave coordinates to
            pts = np.zeros((nQuadPts1D, 2))
            if edge == 0:
                pts[:, 0] = 1 - quadPts1D
                pts[:, 1] = quadPts1D
            elif edge == 1:
                pts[:, 0] = 0
                pts[:, 1] = 1 - quadPts1D

            elif edge == 2:
                pts[:, 0] = quadPts1D
                pts[:, 1] = 0

            for idx, pt in enumerate(pts):
                # the basis function value at each of the quad points
                leftEdgePhi[edge, idx], _ = self.solBasis(pt)

            for idx, pt in enumerate(pts[::-1]):
                # the self.solBasis function value at each of the quad points
                rightEdgePhi[edge, idx], _ = self.solBasis(pt)

        return leftEdgePhi, rightEdgePhi

    def initFreestream(self):
        # calculate conversed qualities
        c = np.sqrt(self.gamma*self.R*self.temp_Inf)

        u = self.mach_Inf*c
        Ub = np.array([self.rho_Inf,
                       self.rho_Inf*u,
                       0.0,
                       self.P_inf/(self.gamma-1) + 0.5*self.rho_Inf*u**2])

        # Ub = np.array([1, 0.5, 0, 2.5])
        self.U = np.zeros((self.mesh.nElem, self.nSolBasis, self.nStates))

        self.U[:, :] = Ub
        self.Ub = Ub
        dg_solver.solver.u = self.U.T
        dg_solver.solver.res = self.U.T

        # print(self.U)

    # def initFromSolve(self, coarseMesh):

    #     for idx, _ in enumerate(coarseMesh.nElem):
    #         # set the four element that were created from uniform refinement
    #         self.U[:, 4*(idx-1):4*(idx)] = coarseMesh.U[idx]

# residuals
    def setResidual(self, U):

        # loop over elements and compute residual contribution from interrior
        self.R = np.zeros(U.shape)
        self.S = np.zeros(self.mesh.nElem)

        self.setInteralResiduals(U)
        # import ipdb
        # ipdb.set_trace()
        self.setEdgeResiduals(U)

    def setInteralResiduals(self, U):

        for idx_elem, elem in enumerate(self.mesh.linElem):

            Uq = np.matmul(self.linPhi, U[elem])
            for qPt in range(self.nLinQuadPts2D):
                # Rtot = 0
                flux = fluxes.analyticflux(Uq[qPt]).T
                for idx_basis in range(self.nSolBasis):
                    self.R[elem, idx_basis] -= np.dot(np.matmul(self.linGPhi[qPt, idx_basis], self.linInvJ[idx_elem]), flux) *\
                        self.linQuadWts2D[qPt]*self.linDetJ[idx_elem]
                # self.R[elem, idx_basis] -= Rtot

        for idx_elem, elem in enumerate(self.mesh.curvElem):

            Uq = np.matmul(self.curvPhi, U[elem])
            for qPt in range(self.nCurvQuadPts2D):
                # Rtot = 0
                flux = fluxes.analyticflux(Uq[qPt]).T
                for idx_basis in range(self.nSolBasis):
                    self.R[elem, idx_basis] -= np.dot(np.matmul(self.curvGPhi[qPt, idx_basis], self.curvInvJ[idx_elem, qPt]), flux) *\
                        self.curvQuadWts2D[qPt]*self.curvDetJ[idx_elem, qPt]
                    #  self.R[elem, idx_basis] -= np.dot(np.matmul(self.linGPhi[qPt, idx_basis], self.linInvJ[idx_elem]), flux) *\
                    #     self.linQuadWts2D[qPt]*self.linDetJ[idx_elem]

                # self.R[elem, idx_basis] -= Rtot
        # return R

    def setEdgeResiduals(self, U):
        for idx_edge in range(self.mesh.nInEdge):
            # ! get the elements connected to the edge

            idx_elem_left = self.mesh.inEdge2Elem[idx_edge, 0]
            idx_edge_left = self.mesh.inEdge2Elem[idx_edge, 1]

            idx_elem_right = self.mesh.inEdge2Elem[idx_edge, 2]
            idx_edge_right = self.mesh.inEdge2Elem[idx_edge, 3]

            uL = np.matmul(self.lLinEdgePhi[idx_edge_left], U[idx_elem_left])
            uR = np.matmul(self.rLinEdgePhi[idx_edge_right], U[idx_elem_right])

            for idx_basis in range(self.nSolBasis):
                # import ipdb
                # ipdb.set_trace()
                Rtot_left = np.zeros(self.nStates)
                Rtot_right = np.zeros(self.nStates)
                Stot_left = 0
                Stot_right = 0
                for q in range(self.nLinQuadPts1D):

                    flux, s = fluxes.roeflux(uL[q], uR[q], self.mesh.inNormal[idx_edge])

                    # flux * delta L * wq
                    tmp = flux*self.mesh.inLength[idx_edge] * self.linQuadWts1D[q]

                    # import ipdb
                    # ipdb.set_trace()
                    Rtot_left += self.lLinEdgePhi[idx_edge_left, q, idx_basis] * tmp
                    Rtot_right += self.rLinEdgePhi[idx_edge_right, q, idx_basis] * tmp

                    Stot_left += s*self.mesh.inLength[idx_edge] * self.linQuadWts1D[q]
                    Stot_right += s*self.mesh.inLength[idx_edge] * self.linQuadWts1D[q]

                self.R[idx_elem_left, idx_basis] += Rtot_left
                self.R[idx_elem_right, idx_basis] -= Rtot_right

                self.S[idx_elem_left] += Stot_left/self.nSolBasis
                self.S[idx_elem_right] += Stot_right/self.nSolBasis

        # print('in edge', self.R)
        # print(self.mesh.bcEdge2Elem)
        for idx_edge in range(self.mesh.nBCEdge):
            idx_elem = self.mesh.bcEdge2Elem[idx_edge, 0]

            idx_edge_loc = self.mesh.bcEdge2Elem[idx_edge, 1]
            bc = self.mesh.bcEdge2Elem[idx_edge, 2]
            # print(idx_elem, idx_edge,  bc)

            if bc == self.curvWall:
                edgePhi = self.lCurvEdgePhi[idx_edge_loc]
                nQuadPts = self.nCurvQuadPts1D
                quadWts = self.curvQuadWts1D
                elem = np.where(self.mesh.curvElem == idx_elem)[0][0]
                # print(bc, idx_edge, idx_elem)
                # import ipdb; ipdb.set_trace()
            else:
                edgePhi = self.lLinEdgePhi[idx_edge_loc]
                nQuadPts = self.nLinQuadPts1D
                quadWts = self.linQuadWts1D

            uL = np.matmul(edgePhi, U[idx_elem])

            # it was written this way to avoid checking the bc type in the loop of basis and q

            if bc == self.wall or bc == self.curvWall:
                for idx_basis in range(self.nSolBasis):
                    Rtot_left = np.zeros(self.nStates)
                    Stot_left = 0

                    for q in range(nQuadPts):

                        if bc == self.curvWall:
                            flux, s = fluxes.wallflux(uL[q], self.curvNormal[elem, idx_edge_loc, q])
                            tmp = self.curvDetJEdge[elem, idx_edge_loc, q]*quadWts[q]
                        else:
                            flux, s = fluxes.wallflux(uL[q], self.mesh.bcNormal[idx_edge])
                            tmp = self.mesh.bcLength[idx_edge]*quadWts[q]

                        Rtot_left += edgePhi[q, idx_basis] * flux * tmp
                        Stot_left += s*tmp/self.nSolBasis

                    self.R[idx_elem, idx_basis] += Rtot_left
                    self.S[idx_elem] += Stot_left

                    # print('wall', bc,  Rtot_left)
                    # if bc == self.curvWall:
                    #     print('wall', elem,  bc == self.curvWall, Rtot_left)

            elif bc == self.inlet:
                for idx_basis in range(self.nSolBasis):
                    Rtot_left = np.zeros(self.nStates)
                    Stot_left = 0

                    for q in range(nQuadPts):

                        flux, s = fluxes.inflowflux(uL[q], self.mesh.bcNormal[idx_edge])

                        # flux * delta L * wq
                        tmp = flux*self.mesh.bcLength[idx_edge] * self.linQuadWts1D[q]

                        Rtot_left += edgePhi[q, idx_basis] * tmp
                        Stot_left += s*self.mesh.bcLength[idx_edge]*self.linQuadWts1D[q]/self.nSolBasis

                    self.R[idx_elem, idx_basis] += Rtot_left
                    self.S[idx_elem] += Stot_left
                    # print('inlet', Rtot_left)

            elif bc == self.outlet:
                for idx_basis in range(self.nSolBasis):
                    Rtot_left = np.zeros(self.nStates)
                    Stot_left = 0
                    for q in range(nQuadPts):

                        flux, s = fluxes.outflowflux(uL[q], self.mesh.bcNormal[idx_edge])

                        # flux * delta L * wq
                        tmp = flux*self.mesh.bcLength[idx_edge] * self.linQuadWts1D[q]

                        Rtot_left += edgePhi[q, idx_basis] * tmp
                        Stot_left += s*self.mesh.bcLength[idx_edge]*self.linQuadWts1D[q]/self.nSolBasis

                    self.R[idx_elem, idx_basis] += Rtot_left
                    self.S[idx_elem] += Stot_left
                    # print('outlet', Rtot_left)

            else:
                print(bc)
                raise NotImplementedError
            # print('bc', bc, 'flux', flux)
#         import ipdb
#         ipdb.set_trace()
# time integration

    def TVDRK2(self, cfl):
        # self.U.reshape()

        # dt = 0.004
        U_FE = np.zeros(self.U.shape)

        # print self.U

        self.setResidual(self.U)
        # print(self.R)
        # quit()
        dt = 2*self.mesh.area*cfl/self.S
        # print dt
        for idx_elem in range(self.mesh.nElem):
            # import ipdb
            # ipdb.set_trace()
            U_FE[idx_elem] = self.U[idx_elem] - dt[idx_elem] * \
                np.matmul(self.invM[idx_elem], self.R[idx_elem])

        self.setResidual(U_FE)
        for idx_elem in range(self.mesh.nElem):
            self.U[idx_elem] = 0.5*(self.U[idx_elem] + U_FE[idx_elem] -
                                    dt[idx_elem] * np.matmul(self.invM[idx_elem], self.R[idx_elem]))

        # print self.U

    def FE(self, cfl):
        # self.U.reshape()

        # dt = 0.004
        # U_FE = np.zeros(self.U.shape)

        # print self.U

        self.setResidual(self.U)
        # print(self.R)
        # quit()

        dt = 2*self.mesh.area*cfl/self.S
        # print dt
        for idx_elem in range(self.mesh.nElem):
            # import ipdb
            # ipdb.set_trace()
            self.U[idx_elem] = self.U[idx_elem] - dt[idx_elem] * \
                np.matmul(self.invM[idx_elem], self.R[idx_elem])
            # print(idx_elem, np.matmul(self.invM[idx_elem], self.R[idx_elem]))
        # print(self.U)
        # print

    def TVDRK3(self, cfl):
        # self.U.reshape()

        # dt = 0.004
        U_1 = np.zeros(self.U.shape)
        U_2 = np.zeros(self.U.shape)

        # print self.U

        self.setResidual(self.U)
        dt = 2*self.mesh.area*cfl/self.S

        # print dt

        for idx_elem in range(self.mesh.nElem):
            U_1[idx_elem] = self.U[idx_elem] - dt[idx_elem] * \
                np.matmul(self.invM[idx_elem], self.R[idx_elem])

        self.setResidual(U_1)
        for idx_elem in range(self.mesh.nElem):
            U_2[idx_elem] = 3.0/4*self.U[idx_elem] + 1.0/4*U_1[idx_elem] - 1.0/4 * dt[idx_elem] * \
                np.matmul(self.invM[idx_elem], self.R[idx_elem])

        self.setResidual(U_2)
        for idx_elem in range(self.mesh.nElem):
            self.U[idx_elem] = 1.0/3*self.U[idx_elem] + 2.0/3*U_2[idx_elem] - 2.0/3 * dt[idx_elem] * \
                np.matmul(self.invM[idx_elem], self.R[idx_elem])

        # print self.U

    def JRK(self, cfl, nStages=4):
        U_stage = np.zeros(self.U.shape)
        # U_2 = np.zeros(self.U.shape)

        # print self.U

        self.setResidual(self.U)
        dt = 2*self.mesh.area*cfl/self.S

        # quit()
        # import ipdb
        # ipdb.set_trace()
        print(np.max(dt))
        for ii in range(nStages, 1, -1):
            # U_stage = self.U
            # print(ii)
            for idx_elem in range(self.mesh.nElem):
                U_stage[idx_elem] = self.U[idx_elem] - dt[idx_elem]/ii * \
                    np.matmul(self.invM[idx_elem], self.R[idx_elem])
            self.setResidual(U_stage)
        # print('U_stage', U_stage)
        # self.U = U_stage

        for idx_elem in range(self.mesh.nElem):
            self.U[idx_elem] = self.U[idx_elem] - dt[idx_elem] * \
                np.matmul(self.invM[idx_elem], self.R[idx_elem])

        # print(self.U)
        # quit()
#
    def solve(self, maxIter=10000, tol=1e-7, cfl=0.95, freeStreamTest=False):

        # solver.cfl = cfl
        # solver_constants.mode = freeStreamTest

        t = time.time()

        # if self.order == 1:
        #     self.Rmax, self.R2 = solver.solve_1storder(maxIter, tol)
        # elif self.order == 2:
        #     solver_constants.recon_bc_flux = self.recon_bc_flux
        # solver.u = np.load('bump0_kfid_2_sol.npz')['q'].T

        #     self.Rmax, self.R2 = solver.solve_2ndorder(maxIter, tol)
        # else:
        #     Exception('order not found')
        for iter in range(maxIter):
            # self.TVDRK3(cfl)
            # self.FE(cfl)
            # self.TVDRK2(cfl)
            self.JRK(cfl, nStages=3)
            # self.FE(cfl)
            print(iter, np.max(np.max(np.max(np.abs(self.R)))))
            self.writeSolution('iter_'+str(iter))

        self.wallTime = time.time() - t

        # self.U = solver.u.T
        # print(self.Rmax[:self.nIter])
        # import ipdb
        # ipdb.set_trace()
        # self.Rmax = self.Rmax[self.Rmax > 0]
        # self.nIter = len(self.Rmax)
        # print('wall time', self.wallTime, 'iters', self.nIter, 'Rmax', self.Rmax[-1])

# postprocess
    def postprocess(self):
        """
            get pressure and M for each cell
            """

        # ------------ total entropy error --------------
        # entropy = np.zeros(self.mesh.nElem)
        # EsTot = 0
        Es = 0
        areaTot = 0

        rhoTot_inf = self.Ptot_inf/(self.R*self.tempTot_inf)
        entropyTot = self.Ptot_inf/rhoTot_inf**self.gamma

        for idx_linElem, idx_elem in enumerate(self.mesh.linElem):

            Uq = np.matmul(self.linPhi, self.U[idx_elem])
            for qPt in range(self.nLinQuadPts2D):
                # Rtot = 0
                # flux = fluxes.analyticflux(Uqq[qPt]).T
                # print(idx_elem)
                p = (self.gamma - 1.)*(Uq[qPt, 3] - 0.5*(np.linalg.norm(Uq[qPt, 1:3])**2)/Uq[qPt, 0])
                entropy = (p / Uq[qPt, 0]**self.gamma)

                Es += (entropy/entropyTot - 1)**2 * self.linQuadWts2D[qPt] * self.linDetJ[idx_linElem]

            areaTot += self.mesh.area[idx_elem]

        for idx_curvElem, idx_elem in enumerate(self.mesh.curvElem):

            Uq = np.matmul(self.curvPhi, self.U[idx_elem])
            for qPt in range(self.nCurvQuadPts2D):
                # Rtot = 0
                # flux = fluxes.analyticflux(Uqq[qPt]).T
                # print(idx_elem)
                p = (self.gamma - 1.)*(Uq[qPt, 3] - 0.5*(np.linalg.norm(Uq[qPt, 1:3])**2)/Uq[qPt, 0])
                entropy = (p / Uq[qPt, 0]**self.gamma)
                Es += (entropy/entropyTot - 1)**2 * self.curvQuadWts2D[qPt] * self.curvDetJ[idx_curvElem, qPt]

            areaTot += self.mesh.area[idx_elem]

        Es = np.sqrt(Es/areaTot)
        print(Es)
        import ipdb
        ipdb.set_trace()
        # quit()
        # for idx_curvElem, idx_elem in enumerate(self.mesh.curvElem):

        # # self.Es = solver_post.getfeildvaribles()

        # # get the edges of the bump
        # # idxs_bumpEdge = np.where(self.mesh.bcEdge2Elem[:, 2] == 1)[0]

        # # initalize the force vector
        # self.F = np.zeros(2)

        # self.cp_wall = np.zeros(idxs_bumpEdge.shape[0])
        # self.x_wall = np.zeros(idxs_bumpEdge.shape[0])

        # # for each cell along the wall
        # for idx, idx_edge in enumerate(idxs_bumpEdge):
        #     length = self.mesh.bcLength[idx_edge]
        #     normal = self.mesh.bcNormal[idx_edge]

        #     nodes = self.mesh.elem2Node[self.mesh.bcEdge2Elem[idx_edge, 0]]
        #     nodes = np.delete(nodes, [self.mesh.bcEdge2Elem[idx_edge, 1]])

        #     self.x_wall[idx] = np.mean(self.mesh.node2Pos[nodes], axis=0)[0]

        #     self.cp_wall[idx] = (self.p_wall[idx] - self.P_inf)
        #     self.F += -1*(self.P_inf - self.p_wall[idx])*normal*length

        # # get state at each edge quad pts on the wall edge of the cell

        # # map the quad pts in reffernce space to global (for plotting x vs Cp)

        # # for second order use du_dx to find the value at the edge
        # if self.order == 1 or not self.recon_p:
        #     # if True:
        #     bump_elem = self.mesh.bcEdge2Elem[idxs_bumpEdge, 0]
        #     # bump_local_edges = self.mesh.bcEdge2Elem[idxs_bumpEdge, 1]
        #     u_wall = self.U[bump_elem, 1:3]
        #     r_wall = self.U[bump_elem, 0]
        #     # import ipdb
        #     # ipdb.set_trace()

        #     unL = np.sum(u_wall*self.mesh.bcNormal[idxs_bumpEdge], axis=1)/r_wall
        #     qL = np.linalg.norm(u_wall, axis=1)/r_wall
        #     utL = np.sqrt(qL**2 - unL**2)

        #     self.p_wall = (self.gamma-1)*(self.U[bump_elem, 3] - 0.5*r_wall*utL**2)

        #     # self.p_wall = solver_post.p[self.mesh.bcEdge2Elem[idxs_bumpEdge, 0]]
        # else:
        #     # self.p_wall = solver_post.getwallpressure(idxs_bumpEdge+1)
        # for idx, idx_edge in enumerate(idxs_bumpEdge):
        #     length = self.mesh.bcLength[idx_edge]
        #     normal = self.mesh.bcNormal[idx_edge]

        #     nodes = self.mesh.elem2Node[self.mesh.bcEdge2Elem[idx_edge, 0]]
        #     nodes = np.delete(nodes, [self.mesh.bcEdge2Elem[idx_edge, 1]])

        #     self.x_wall[idx] = np.mean(self.mesh.node2Pos[nodes], axis=0)[0]

        #     self.cp_wall[idx] = (self.p_wall[idx] - self.P_inf)
        #     self.F += -1*(self.P_inf - self.p_wall[idx])*normal*length

        # h = 0.0625
        # self.cp_wall /= (self.gamma/2*self.P_inf*self.machInf**2)
        # self.cd, self.cl = self.F/(self.gamma/2*self.P_inf*self.machInf**2*h)

        # print('cd', self.cd, 'cl', self.cl, 'Es', self.Es)
        # idxs_sorted = np.argsort(self.x_wall)
        # self.x_wall = self.x_wall[idxs_sorted]
        # self.p_wall = self.p_wall[idxs_sorted]
        # self.cp_wall = self.cp_wall[idxs_sorted]

    def plotResiduals(self):
        plt.semilogy(range(1, self.nIter+1), self.Rmax, label='order: ' + str(self.order))
        plt.xlabel('Iteration', fontsize=16)
        plt.ylabel(r'$|R_{\infty }|$',  fontsize=16)
        plt.title('Residual History',  fontsize=16)

    def plotCP(self):
        plt.plot(self.x_wall, self.cp_wall, '-',  label='order: ' + str(self.order))
        plt.xlabel(r'$X$', fontsize=16)
        plt.ylabel(r'$C_p$', fontsize=16)
        plt.title(r'$C_p$ along the bump')
        plt.gca().invert_yaxis()

    def writeSolution(self, fileName):
        # def writeLine(cmd)

        # so all the array values are printed
        np.set_printoptions(threshold=np.inf)

        # for each set of elements of the same geoemtric order
        with open(fileName + '.dat', 'w') as fid:

            fid.write('TITLE = "bump"\n')
            # fid.write('Variables="X", "Y", "U", "V", "rho", "P", "M", "rhoRes"\n')
            fid.write('Variables="X", "Y", "U", "V", "rho", "Index"\n')

            elemOrder = {
                self.mesh.curvOrder: np.array([], dtype=int),
                1: self.mesh.linElem,
            }
            elemOrder[self.mesh.curvOrder] = np.append(elemOrder[self.mesh.curvOrder], self.mesh.curvElem)
            # self.mesh.curvOrder: self.mesh.curvElem

            for q in elemOrder.keys():

                # get the basis functions for the mapping
                # this is a little bit of extra work for the linear elements, but by not utilizing the constant jacobian
                #  the next loop can be written without conditional statments (if q==1) which is nice and clean

                #
                nBasis, basis = quadrules.getTriLagrangeBasis2D(q)

                # these are the points(in reference space) where the function will be evaluated
                interpOrder = min([self.order+3, q+4])
                Xi = quadrules.getTriLagrangePts2D(interpOrder)

                # create element connectivity
                N = len(Xi)

                nodes = np.arange(1, N+1)

                rows = []
                k = 0
                for ii in range(1, interpOrder+1)[::-1]:
                    rows.append(nodes[k:k+ii+1])
                    k += ii+1

                # conn = np.zeros(interpOrder**2, 3)
                conn = []
                # idx_conn = 0
                for idx, row in enumerate(rows):
                    # do add the top and bottom elements to the connectivity matrix
                    k = len(row)

                    if idx > 0:
                        # add the elements below this row of nodes
                        for i in range(k-1):
                            conn.append([row[i], row[i+1], row[i]-k])

                    # add the elements above the row
                    for i in range(k-1):
                        conn.append([row[i], row[i+1], row[i+1]+k-1])
                conn = np.array(conn)
                # import ipdb
                # ipdb.set_trace()

                geomPhi = np.zeros((len(Xi), nBasis))
                solPhi = np.zeros((len(Xi), self.nSolBasis))

                for idx, pt in enumerate(Xi):
                    # the basis function value at each of the quad points
                    geomPhi[idx], _ = basis(pt)
                    solPhi[idx], _ = self.solBasis(pt)

                # loop over elements now
                for idx_elem, elem in enumerate(elemOrder[q]):
                    # nodesPos = self.mesh.node2Pos[self.mesh.elem2Node[elem]]
                    if q == 1:
                        nodesPos = self.mesh.node2Pos[self.mesh.elem2Node[elem]]
                    else:
                        nodesPos = self.mesh.curvNodes[idx_elem]
                    Upts = np.matmul(solPhi, self.U[elem])
                    Xpts = np.matmul(geomPhi, nodesPos)

                    # writeZone()
                    # print(elem)
                    fid.write('ZONE\n')
                    fid.write('T="elem '+str(elem)+'"\n')
                    fid.write('DataPacking=Block\n')
                    fid.write('ZoneType=FETRIANGLE\n')
                    fid.write('N=' + str(N) +
                              ' E=' + str(interpOrder**2)+'\n')
                    # fid.write('VarLocation=([3-9]=CellCentered)\n')

                    fid.write('#XData (Grid Variables must be nodal)\n')
                    fid.write(str(Xpts[:, 0])[1:-1]+'\n')
                    fid.write('#YData (Grid Variables must be nodal)\n')
                    fid.write(str(Xpts[:, 1])[1:-1]+'\n')

                    fid.write('#U Data \n')
                    fid.write(str(Upts[:, 1]/Upts[:, 0])[1:-1]+'\n')

                    fid.write('#V Data \n')
                    fid.write(str(Upts[:, 2]/Upts[:, 0])[1:-1]+'\n')

                    fid.write('#rho Data \n')
                    fid.write(str(Upts[:, 0])[1:-1]+'\n')

                    # fid.write('#P Data \n')
                    # fid.write(str(solver_post.p)[1:-1]+'\n')
                    # fid.write('#M Data \n')
                    # fid.write(str(solver_post.mach)[1:-1]+'\n')
                    # fid.write('#rhoRes Data \n')
                    # fid.write(str(solver.res[0, :])[1:-1]+'\n')

                    fid.write('#number Data \n')
                    fid.write(str(np.ones(N)*elem+1)[1:-1]+'\n')

                    fid.write('#Connectivity List\n')
                    for idx in range(len(conn)):
                        fid.write(str(conn[idx])[1:-1]+'\n')

        # set np back to normal
        np.set_printoptions(threshold=1000)


if __name__ == '__main__':
    # bump = Mesh('meshes/test0_21.gri')

    def flatWall(x):
        return -x*(x-1)*0.2
        # return 0
        # return x*0.1

    def bumpShape(x):
        return 0.0625*np.exp(-25*x**2)

    bump = Mesh('meshes/bump0_kfid.gri', wallGeomFunc=bumpShape)
    # bump = Mesh('meshes/twoElem.gri', wallGeomFunc=flatWall, check=True)
    # bump = Mesh('meshes/threeElem.gri', wallGeomFunc=flatWall, check=True)
    # bump = Mesh('meshes/test0_2.gri', wallGeomFunc=flatWall, check=True)
    # bump = Mesh('meshes/refElem.gri', wallGeomFunc=flatWall)
    bump.refine()
    bump.refine()
    # bump.refine()

    # test = Mesh('meshes/test0_2.gri', wallGeomFunc=flatWall)
    DGSolver = DGSolver(bump, order=1)

    # DGprint(FVSolver.getResidual())
    # DGSolver.solve(maxIter=10, cfl=0.5)
    dg_solver.solver.solve_jrk(75000, 1e-7, 0.45, 4)
    DGSolver.U = dg_solver.solver.u.T
    DGSolver.postprocess()
    # DGSolver.writeSolution('test_multi_2')
    # import ipdb
    # ipdb.set_trace()
    # t = time.time()

    # print(DGSolver.wallTime)
    # print(time.time() - t)
    # DGFVSolver.
    # DGFVSolver.solve()
    # DGFVSolver.postprocess()
    # DGFVSolver.writeSolution('lv0')
    # DGFVSolver.plotCP()
    # DGFVSolver.plotResiduals()