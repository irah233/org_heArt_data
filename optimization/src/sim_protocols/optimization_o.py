from dolfin import *
from dolfin_adjoint import *
import numpy as np
import os
import math
import csv
from pyadjoint.placeholder import Placeholder
from ..optimization.MyReducedFunctional2 import ReducedFunctional
from pyadjoint.reduced_functional_numpy import ReducedFunctionalNumPy 
from ..sim_protocols.heArtsolver_clean_o import heArtsolver_clean
from collections import OrderedDict
from mpi4py import MPI as pyMPI
import matplotlib as mpl
if os.environ.get('DISPLAY','') == '':
    print('no display found. Using non-interactive Agg backend')
    mpl.use('Agg')
from matplotlib import pylab as plt


def GetTmax(Tmax_):

    comm = MPI.comm_world
    rank = comm.Get_rank()
    N = Tmax_.vector().size()   # length of Tmax

    try:
        val_local_sum = np.sum(Tmax_.vector().get_local()[:])    # sum of Tmax 
    except IndexError:
        val_local_sum = 0.0

    Tmax_avg = MPI.sum(comm, val_local_sum)/N
    Tmax_arr = Tmax_.vector().get_local()[:]

    try:
        val_local_diff_arr = (Tmax_.vector().get_local()[:] - Tmax_avg)
        val_local_sum_diff_sq = np.sum(np.multiply(val_local_diff_arr, val_local_diff_arr))
    except IndexError:
        val_local_sum_diff_sq = 0.0

    Tmax_std = math.sqrt(MPI.sum(comm, val_local_sum_diff_sq)/N)

    return Tmax_avg, Tmax_std, Tmax_arr


def printTmax(Tmax_array):

    comm = MPI.comm_world
    rank = comm.Get_rank()

    if(rank ==0):
        print("Tmax = ")

    for m1 in Tmax_array:
        Tmax_avg, Tmax_std, Tmax_arr = GetTmax(m1)
        
        if(rank == 0):
            print("m_avg = %15.10f , m_std = %15.10f" % (Tmax_avg, Tmax_std), flush=True)

def eval_cb_pre(m):

    comm = MPI.comm_world
    rank = comm.Get_rank()

    printTmax(m)


def eval_cb(j, m):

    comm = MPI.comm_world
    rank = comm.Get_rank()

    print("j = %10.7f" %(j), flush=True)
    printTmax(m)

def readmesh(parameters):

    comm = MPI.comm_world
    rank = comm.Get_rank()

    mesh = Mesh() 
    directory = parameters["directory_me"]
    casename = parameters["casename"]
    outputfolder = parameters["outputfolder"]
    
    meshfilename = directory + casename + ".hdf5"
    f = HDF5File(MPI.comm_world, meshfilename, 'r')
    f.read(mesh, casename, False)
    
    facetboundaries = MeshFunction("size_t", mesh, 2)
    f.read(facetboundaries, casename+"/"+"facetboundaries")
    
    edgeboundaries = MeshFunction("size_t", mesh, 1)
    f.read(edgeboundaries, casename+"/"+"edgeboundaries")
    
    deg = 4
    VQuadelem = VectorElement("Quadrature", 
                              mesh.ufl_cell(), 
                              degree=deg, 
                              quad_scheme="default")
    VQuadelem._quad_scheme = 'default'
    
    fiberFS = FunctionSpace(mesh, VQuadelem)
    
    f0 = Function(fiberFS)
    s0 = Function(fiberFS)
    n0 = Function(fiberFS)

    eC0 = Function(fiberFS)
    eL0 = Function(fiberFS)
    eR0 = Function(fiberFS)
    
    f.read(f0, casename+"/"+"eF")
    f.read(s0, casename+"/"+"eS")
    f.read(n0, casename+"/"+"eN")

    f.read(eC0, casename+"/"+"eC")
    f.read(eL0, casename+"/"+"eL")
    f.read(eR0, casename+"/"+"eR")
		
    f0 = f0/sqrt(inner(f0, f0))
    s0 = s0/sqrt(inner(s0, s0))
    n0 = n0/sqrt(inner(n0, n0))

    eC0 = eC0/sqrt(inner(eC0, eC0))
    eL0 = eL0/sqrt(inner(eL0, eL0))
    eR0 = eR0/sqrt(inner(eR0, eR0))

    matid = MeshFunction("size_t", mesh, mesh.topology().dim(), 0)
    f.read(matid, casename+"/"+"matid")

    return mesh, facetboundaries, edgeboundaries, f0, s0, n0, eC0, eL0, eR0, matid

def optimization(IODet, SimDet):

    tape = Tape()
    set_working_tape(tape)

    mesh, facetboundaries, edgeboundaries, f0, s0, n0, eC0, eL0, eR0, matid = readmesh(IODet)
   

    # Read in PV data
    PVloopdatafilename = IODet["PVloop_data_file"]
    LVPtargets = np.load(PVloopdatafilename, allow_pickle=True)["LVP"]#[0:20]
    LVVtargets = np.load(PVloopdatafilename, allow_pickle=True)["LVV"]#[0:20]
    ndatapts = len(LVPtargets)

    # Read in Strain data 
    if("Strain_data_file" in IODet.keys()):
        Straindatafilename = IODet["Strain_data_file"]
        eCCtargets = np.load(Straindatafilename, allow_pickle=True)["Ecc"]#[:,0:20]
        eLLtargets = np.load(Straindatafilename, allow_pickle=True)["Ell"]#[:,0:20]
    else:
        eCCtargets = None
        eLLtargets = None

    assert (len(eCCtargets.T) == ndatapts), "Number of strain data point is different from PV data points"

    # Get Output directory and mkdir if it does not exist
    outdir = IODet["outputfolder"]
    if not os.path.exists(outdir):
        os.makedirs(outdir)

    # Get Initial value (assume constant)
    if("init_opt_val" in SimDet.keys()):
        init_val_peak = float(SimDet["init_opt_val"])      # initial value set in case file
    else:
        init_val_peak = 0.0

    Rspace = FunctionSpace(mesh, "DG", 0)
    Tmax_ctrls = OrderedDict()
    if("Initfile" in IODet.keys()):
        fdata = HDF5File(MPI.comm_world, IODet["Initfile"], 'r')

    for p in range(0,ndatapts):                            # number of data points

        init_val = init_val_peak                           # initial Tmax  5000.0
        Tmax_ctrls[p] = Function(Rspace)                   # DG Lagrange    f_37
        Tmax_ctrls[p].assign(interpolate(Constant(init_val), Rspace), annotate=False)     #f_37

        if("Initfile" in IODet.keys()):
            fdata.read(Tmax_ctrls[p], "Tmax"+str(p))
            #print(GetTmax(Tmax_ctrls[p]))      # len of Tmax_arr = 431
            #Tmax_avg, Tmax_std, Tmax_arr = GetTmax(Tmax_ctrls[p])


    if("Initfile" in IODet.keys()):
        fdata.close()

    if("UpperBd" in SimDet.keys()):
        UpperBd = float(SimDet["UpperBd"])
    else:
        UpperBd = 500e3

    if("LowerBd" in SimDet.keys()):
        LowerBd = float(SimDet["LowerBd"])
    else:
        LowerBd = 0.0


    Tmax_bds = [[LowerBd]*ndatapts, [UpperBd]*ndatapts]

    w, j, LVP_array, LVV_array, eCC_array, eLL_array, eRR_array, Tmaxarray2 = heArtsolver_clean(Tmax_ctrls, LVVtargets, LVPtargets, mesh, facetboundaries, edgeboundaries, f0, s0, n0, eC0, eL0, eR0, matid, SimDet, IODet, eCCtargets=eCCtargets, eLLtargets=eLLtargets)

    # Regularization
    '''
    alpha = Constant(1e-8)
    regularisation = alpha/2*sum([(fb-fa)**2*dx for fb, fa in \
            zip(list(Tmax_ctrls.values())[1:], list(Tmax_ctrls.values())[:-1])])
    '''
    J = j #+ assemble(regularisation)

    m1 = [Control(Tmax_ctrl) for Tmax_ctrl in Tmax_ctrls.values()]
    rf = ReducedFunctional(J, m1, eval_cb_post = eval_cb, eval_cb_pre = eval_cb_pre, debugfile=outdir+"Log.hdf5")
    rf_np = ReducedFunctionalNumPy(rf)
    f_opt = minimize(rf_np, bounds = Tmax_bds, method="L-BFGS-B", options={"maxiter": 50, "pgtol":1e-9, "factr":0.0})

    printTmax(f_opt)


    w, J, LVP_array, LVV_array, eCC_array, eLL_array, eRR_array, Tmaxarray = heArtsolver_clean(f_opt, LVVtargets, LVPtargets, mesh, facetboundaries, edgeboundaries, f0, s0, n0, eC0, eL0, eR0, matid, SimDet, IODet, eCCtargets=eCCtargets, eLLtargets=eLLtargets, isannotate=False)

 
    plt.figure(1)
    plt.plot(LVV_array, LVP_array)
    plt.plot(LVVtargets, LVPtargets, '*')
    plt.xlabel("LVV (ml)")
    plt.ylabel("LVP (mmHg)")
    plt.savefig(outdir+"PVloop.png")

    plt.figure(2)
    Tmax_avg_array = []
    Tmax_std_array = []
    for T in f_opt:
    #for T in Tmaxarray2:
        Tmax_avg, Tmax_std, Tmax_arr = GetTmax(T)
        Tmax_avg_array.append(Tmax_avg)
        Tmax_std_array.append(Tmax_std)
    plt.errorbar(np.arange(0,len(Tmax_avg_array)), Tmax_avg_array, Tmax_std_array)
    plt.xlabel("Time point")
    plt.ylabel("Tmax (Pa)")
    plt.savefig(outdir+"Tmax.png")    

    Tmax_arr_array = []
    cnt = 0
    for T in f_opt:
        File(outdir+"Tmax"+str(cnt)+".pvd") << T
        Tmax_avg, Tmax_std, Tmax_arr = GetTmax(T)
        Tmax_arr_array.append(Tmax_arr)
        cnt += 1


    cnt = 0;
    Tmax_arr_array = np.array(Tmax_arr_array)

    Tmaxarray = np.array(Tmaxarray)
    
    with open(outdir+'regional_Tmax.csv','w',newline='') as fs:
      aa=[]
      fieldnames=[]
      plt.figure(3)
      fig, axs = plt.subplots(4, 4)
      for p in range(0, len(eLLtargets)):
        i = int(cnt/4)
        j = cnt%4
        cnt += 1
        axs[i,j].plot(np.arange(0,len(Tmax_arr_array.T[p])), Tmax_arr_array.T[p]*0.0075, '-')
        axs[i,j].set_title("Sector = " + str(cnt), fontsize=8)
        fieldnames.append('Sector'+'Tmax'+str(cnt))
        thewriter = csv.DictWriter(fs, fieldnames=fieldnames)
        aa.append((Tmax_arr_array.T[p]*0.0075).tolist())


      plt.xlabel("Time point")
      plt.ylabel("Tmax (mmHg)")
      fig.tight_layout()
      plt.savefig(outdir+"Tmax_region.png")
      
      n=0
      while n!=(len(aa[5])):
        thewriter.writerow({fieldnames[0]:aa[0][n],fieldnames[1]:aa[1][n],fieldnames[2]:aa[2][n],\
        fieldnames[3]:aa[3][n],fieldnames[4]:aa[4][n],fieldnames[5]:aa[5][n],fieldnames[6]:aa[6][n],\
        fieldnames[7]:aa[7][n],fieldnames[8]:aa[8][n],fieldnames[9]:aa[9][n],fieldnames[10]:aa[10][n],\
        fieldnames[11]:aa[11][n],fieldnames[12]:aa[12][n],fieldnames[13]:aa[13][n],fieldnames[14]:aa[14][n],\
        fieldnames[15]:aa[15][n]})
        n+=1

    with open(outdir+'total_Strain.csv','w',newline='') as f:   
        aa=[]
        fieldnames=[]
        cnt = 0;
        eCC_array = np.array(eCC_array)
        plt.figure(4)
        fig, axs = plt.subplots(4, 4)
        for p in range(0, len(eCCtargets)):
            i = int(cnt/4)
            j = cnt%4
            cnt += 1
            axs[i,j].plot(np.arange(0,len(eCCtargets[p])), eCCtargets[p], '*')
            axs[i,j].plot(np.arange(0,len(eCC_array.T[p])), eCC_array.T[p], '-')
            axs[i,j].set_title("Sector = " + str(cnt), fontsize=8)
            fieldnames.append('Ecc'+'m'+str(cnt))
            fieldnames.append('Ecc'+'s'+str(cnt))
            thewriter = csv.DictWriter(f, fieldnames=fieldnames)
            aa.append(eCCtargets[p].tolist())
            aa.append(eCC_array.T[p].tolist())

        plt.xlabel("Time point")
        plt.ylabel("Ecc")
        fig.tight_layout()
        plt.savefig(outdir+"Ecc.png")

        cnt = 0;
        eLL_array = np.array(eLL_array)
        plt.figure(5)
        fig, axs = plt.subplots(4, 4)
        for p in range(0, len(eLLtargets)):
            i = int(cnt/4)
            j = cnt%4
            cnt += 1
            axs[i,j].plot(np.arange(0,len(eLLtargets[p])), eLLtargets[p], '*')
            axs[i,j].plot(np.arange(0,len(eLL_array.T[p])), eLL_array.T[p], '-')
            axs[i,j].set_title("Sector = " + str(cnt), fontsize=8)
            fieldnames.append('Ell'+'m'+str(cnt))
            fieldnames.append('Ell'+'s'+str(cnt))
            thewriter = csv.DictWriter(f, fieldnames=fieldnames)
            aa.append(eLLtargets[p].tolist())
            aa.append(eLL_array.T[p].tolist())

        plt.xlabel("Time point")
        plt.ylabel("Ell")
        fig.tight_layout()
        plt.savefig(outdir+"Ell.png")
        m=0
        while m!=(len(aa[5])):                 
                thewriter.writerow({fieldnames[0]:aa[0][m],fieldnames[1]:aa[1][m],fieldnames[2]:aa[2][m],\
                fieldnames[3]:aa[3][m],fieldnames[4]:aa[4][m],fieldnames[5]:aa[5][m],fieldnames[6]:aa[6][m],\
                fieldnames[7]:aa[7][m],fieldnames[8]:aa[8][m],fieldnames[9]:aa[9][m],fieldnames[10]:aa[10][m],\
                fieldnames[11]:aa[11][m],fieldnames[12]:aa[12][m],fieldnames[13]:aa[13][m],fieldnames[14]:aa[14][m],\
                fieldnames[15]:aa[15][m],fieldnames[16]:aa[16][m],fieldnames[17]:aa[17][m],fieldnames[18]:aa[18][m],\
                fieldnames[19]:aa[19][m],fieldnames[20]:aa[20][m],fieldnames[21]:aa[21][m],fieldnames[22]:aa[22][m],\
                fieldnames[23]:aa[23][m],fieldnames[24]:aa[24][m],fieldnames[25]:aa[25][m],fieldnames[26]:aa[26][m],\
                fieldnames[27]:aa[27][m],fieldnames[28]:aa[28][m],fieldnames[29]:aa[29][m],fieldnames[30]:aa[30][m],\
                fieldnames[31]:aa[31][m],fieldnames[32]:aa[32][m],fieldnames[33]:aa[33][m],fieldnames[34]:aa[34][m],\
                fieldnames[35]:aa[35][m],fieldnames[36]:aa[36][m],fieldnames[37]:aa[37][m],fieldnames[38]:aa[38][m],\
                fieldnames[39]:aa[39][m],fieldnames[40]:aa[40][m],fieldnames[41]:aa[41][m],fieldnames[42]:aa[42][m],\
                fieldnames[43]:aa[43][m],fieldnames[12+32]:aa[12+32][m],fieldnames[13+32]:aa[13+32][m],\
                fieldnames[14+32]:aa[14+32][m],fieldnames[15+32]:aa[15+32][m],fieldnames[16+32]:aa[16+32][m],\
                fieldnames[17+32]:aa[17+32][m],fieldnames[18+32]:aa[18+32][m],fieldnames[19+32]:aa[19+32][m],\
                fieldnames[20+32]:aa[20+32][m],fieldnames[21+32]:aa[21+32][m],fieldnames[22+32]:aa[22+32][m],\
                fieldnames[23+32]:aa[23+32][m],fieldnames[24+32]:aa[24+32][m],fieldnames[25+32]:aa[25+32][m],\
                fieldnames[26+32]:aa[26+32][m],fieldnames[27+32]:aa[27+32][m],fieldnames[28+32]:aa[28+32][m],\
                fieldnames[29+32]:aa[29+32][m],fieldnames[30+32]:aa[30+32][m],fieldnames[31+32]:aa[31+32][m]})
                m+=1

