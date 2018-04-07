import numpy as np
from scipy.sparse import coo_matrix, csc_matrix, csr_matrix, spdiags, eye
import scipy.io as sio

from fealpy.functionspace.vem_space import VirtualElementSpace2d 
from fealpy.vemmodel import doperator
from scipy.sparse.linalg import spsolve

from fealpy.mesh.tree_data_structure import Quadtree 
from fealpy.quadrature import TriangleQuadrature 
#from fealpy.mesh.simple_mesh_generator import rectangledomainmesh

from fealpy.vemmodel.integral_alg import PolygonMeshIntegralAlg

class SCFTParameter():
    def __init__(self):
        self.Nspecies = 2
        self.Nblend   = 1
        self.Nblock   = 2
        self.Ndeg     = 100
        self.fA       = 0.2
        self.chiAB    = 0.25
        self.dim      = 2
        self.dtMax    = 0.005
        self.tol      = 1.0e-6
        self.maxit    = 5000
        self.showstep = 200
        self.pdemethod = 'CN'
        self.integrator = TriangleQuadrature(3)
                
    #def init_mesh(self):
    #   node = np.array([
    #       (0, 0),
    #       (1, 0),
    #       (1, 1),
    #       (0, 1)], dtype=np.float)
    #   
    #   cell = np.array([0,1,2,3])
    #   mesh = Quadtree(node,cell)
    #   return mesh

       


class TimeLine():
    def __init__(self, interval, dt):
        self.T0 = interval[0]
        self.T1 = interval[1]
        self.Nt = int(np.ceil((self.T1 - self.T0)/dt))
        self.dt = (self.T1 - self.T0)/self.Nt
        self.current = 0

    def get_index_interval(self):
        return np.arange(0, self.Nt+1) 

    def get_number_of_time_steps(self):
        return self.Nt+1

    def get_current_time_step(self):
        return self.current

    def get_time_step_length(self):
        return self.dt

    def stop(self):
        return self.current >= self.Nt - 1 

    def step(self):
        self.current += 1

    def reset(self):
        self.current = 0

class PDESolver():
    def __init__(self, vemspace, measure, method ='CN'):

        self.method = method
        self.vemspace = vemspace
        self.measure = measure
        
        self.mat = doperator.basic_matrix(self.vemspace, self.measure)
        self.A = doperator.stiff_matrix(self.vemspace, self.measure, mat=self.mat)
        self.M = doperator.mass_matrix(self.vemspace, self.measure, mat=self.mat)

    def get_current_linear_system(self, u0, dt):
        M = self.M
        S = self.A
        F = self.F
        
        if self.method is 'FM':
            b = -dt*(S + F)@u0 + M@u0
            A = M
            return A, b
        if self.method is 'BM':
            b = M@u0
            A = M + dt*(S + F)
            return A, b
        if self.method is 'CN':
            b = -0.5*dt*(S + F)@u0 + M@u0
            A = M + 0.5*dt*(S + F)
            return A, b

    def run(self,timeline, uh, F):
        self.F = F
        
        while timeline.current < timeline.Nt:
            current = timeline.current
            dt = timeline.get_time_step_length()
            A, b = self.get_current_linear_system(uh[:, current], dt)
            uh[:, current+1] = spsolve(A, b)
            print(uh[:, current+1])
            timeline.current +=1
        timeline.reset()


class SCFTVEMModel():
    def __init__(self,mesh,option,p=1):
        self.vemspace =VirtualElementSpace2d(mesh, p)
        self.mesh  = self.vemspace.mesh
        
        self.uh = self.vemspace.function()
        self.area = self.vemspace.smspace.area
        self.totalArea = np.sum(self.area)
        self.option = option
        #TODO
        self.integralalg = PolygonMeshIntegralAlg(option.integrator,
                self.mesh,
                self.area,
                barycenter=self.vemspace.smspace.barycenter)
        
        self.timeline0 = TimeLine([0, option.fA], option.dtMax)
        self.timeline1 = TimeLine([option.fA, 1], option.dtMax)

        N = self.timeline0.Nt + self.timeline1.Nt+1
       
        self.q0 = self.vemspace.function(dim=N)
        self.q1 = self.vemspace.function(dim=N)
        self.Ndof = self.vemspace.number_of_global_dofs()

        self.rho = [self.vemspace.function() for i in range(option.Nspecies)]
        self.w = [self.vemspace.function() for i in range(option.Nspecies)]
        self.mu = [self.vemspace.function() for i in range(option.Nspecies)]

        self.grad = self.vemspace.function(dim=option.Nspecies)
        self.sQ = np.zeros((option.Nspecies -1, option.Nblend))

        self.solver = PDESolver(self.vemspace,self.area, option.pdemethod)


    def initialize(self):
        option = self.option
        fields = option.fields
        chiN = option.chiAB * option.Ndeg

        self.w[0][:] = fields[:, 0] - fields[:, 1]
        self.w[1][:] = fields[:, 1] + fields[:, 1]


        self.mu[0][:] = 0.5*(self.w[0] + self.w[1])
        self.mu[1][:] = 0.5*(self.w[0] - self.w[1])

        self.rho[0][:] = 0.5 + self.mu[1]/chiN
        self.rho[1][:] = 1.0 - self.rho[0]
        
        self.data = {
                'node':self.mesh.node,
                'elem':self.mesh.ds.cell+1,
                'rhoA':[self.rho[0]],
                'rhoB':[self.rho[1]],
                'sQ':[],
                'ediff':[],
                'H':[]
                }

    def project_to_smspace(self, uh):
        p = self.vemspace.p
        cell2dof, cell2dofLocation = self.vemspace.dof.cell2dof, self.vemspace.dof.cell2dofLocation
        cd = np.hsplit(cell2dof, cell2dofLocation[1:-1])
        g = lambda x: x[0]@self.uh[x[1]]
        S = self.vemspace.smspace.function()
        S[:] = np.concatenate(list(map(g, zip(self.solver.mat.PI1, cd))))
        return S

    def update_propagator(self):
        option = self.option
        n0 = self.timeline0.get_number_of_time_steps()
        n1 = self.timeline1.get_number_of_time_steps()
        

        #TODO self.PI0
        integral = self.integralalg.integral
        
        S = self.project_to_smspace(self.w[0])
        F0 = doperator.cross_mass_matrix(
                integral,
                S.value,
                self.vemspace,
                self.area,
                self.solver.mat.PI0)

        S = self.project_to_smspace(self.w[1])
        F1 = doperator.cross_mass_matrix(
                integral,
                S.value,
                self.vemspace,
                self.area,
                self.solver.mat.PI0)
        
        self.q0[:, 0] = 1.0
        print("q0 first step")
        self.solver.run(self.timeline0,self.q0[:, 0:n0], F0)
        print("q0 second step")
        self.solver.run(self.timeline1, self.q1[:, n0-1:], F1)
        self.q1[:, 0] = 1.0
        print("q1 first step")
        self.solver.run(self.timeline1, self.q1[:, 0:n1], F1)
        print("q1 second step")
        self.solver.run(self.timeline0, self.q1[:, n1-1:], F0)


    def integral_time(self, q, dt):
        f = -0.625*(q[:, 0] + q[:, -1]) + 1/6*(q[:, 1] + q[:, -2]) - 1/24*(q[:, 2] + q[:, -3])
        f += np.sum(q, axis=1)
        f *= dt
        return f

    def integral_space(self, uh):
        S = self.project_to_smspace(uh)
        Q = self.integralalg.integral(S.value)
        return Q


    def update_density(self, q):
        n0 = self.timeline0.get_number_of_time_steps()
        self.rho[0][:] = self.integral_time(q[:, 0:n0],
                self.timeline0.dt)/self.sQ[0, 0]
        self.rho[1][:] = self.integral_time(q[:, n0-1:],
                self.timeline1.dt)/self.sQ[0, 0]

    def update_singleQ(self, integrand):
        f = self.integral_space(integrand)/self.totalArea 
        return f

    def update_hamilton(self):
        u = self.mu[0]
        mu1_int = self.integral_space(u)

        S = self.project_to_smspace(self.mu[1])
        def u(x, cellidx):
            val = S.value(x, cellidx=cellidx)**2
            return val 

        mu2_int = self.integral_space(u)

        chiN = self.option.chiAB * self.option.Ndeg
        H = -mu1_int + mu2_int/chiN
        return H

    def update_field(self):

        option = self.option
        chiN = option.chiAB * option.Ndeg
        
        lambd = np.array([2,2])

        self.grad[:, 0] = self.rho[0]  + self.rho[1] - 1.0
        self.grad[:, 1] = 2.0*self.mu[1]/chiN - self.rho[0] + self.rho[1]

        err = np.max(np.abs(self.grad), axis=0)

        self.mu[0] += lambd[0]*self.grad[:, 0]
        self.mu[1] -= lambd[1]*self.grad[:, 1]

        self.w[0][:] = self.mu[0] - self.mu[1] 
        self.w[1][:] = self.mu[0] + self.mu[1]
        
        return err 

    def find_saddle_point(self, datafile='data', showsolution=True, file_path=None):
        
        self.res = np.inf
        self.Hold = np.inf
        self.ediff = np.inf
        iteration = 0
        option = self.option
        
        if file_path is not None:
            f = open(file_path + '/log.txt', 'w')
        while (self.res > option.tol) and (iteration < option.maxit):
            self.H, self.res = self.one_step()
            self.ediff = self.H - self.Hold
            self.Hold = self.H
            iteration += 1


            if file_path is not None:
                string = 'Iter: %d ======> \n sQ: %.15e res: %.15e ediff: %.15e H: %f \n' % (iteration, self.sQ, self.res, self.ediff, self.H)
                f.write(string)

            print('Iter:',iteration,'======>','res:', self.res, 'ediff:',self.ediff, 'H:', self.H)
            print('\n')

            self.data['rhoA'].append(self.rho[0])
            self.data['rhoB'].append(self.rho[1])
            self.data['H'].append(self.H)
            self.data['ediff'].append(self.ediff)

            if (iteration%option.showstep == 0) and showsolution:
                self.show_solution(iteration)
        sio.matlab.savemat(datafile+'.mat', self.data)
        
        if file_path is not None:
            f.close()
        
    def one_step(self):
        self.update_propagator() 
        qq = self.q0*self.q1[:, -1::-1] 
        print(self.sQ)
        self.sQ[0,0] = self.update_singleQ(self.q0.index(-1))
        print('sQ:', self.sQ)
        self.data['sQ'].append(self.sQ[0, 0])

        self.update_density(qq)

        error = self.update_field()
    
        H = self.update_hamilton()

        H = H/self.totalArea - np.log(self.sQ[0,0])
        return H, error.max()


    def get_partial_F(self, radius):
 
        self.update_propagator()

        qq = self.q0 * self.q1[:, -1::-1] 
        self.sQ[0,0] = self.updateQ(self, qq)
        gradF = -np.sum(sQ.reshape(-1))

        return partialF

    #def show_solution(self, i):
    #    mesh = self.mesh.mesh
    #    cell = mesh.ds.cell
    #    node = mesh.node
    #    c = self.rho[0].view(np.ndarray)
    #    c = np.sum(c[cell], axis=1)/3
    #    c = cs.val_to_color(c)
    #    fig = FF.create_trisurf(
    #            x = node[:, 0], 
    #            y = node[:, 1],
    #            z = node[:, 2],
    #            show_colorbar = True,
    #            plot_edges=False,
    #            simplices=cell)
    #    fig['data'][0]['facecolor'] = c
    #    py.plot(fig, filename='test{}'.format(i))












