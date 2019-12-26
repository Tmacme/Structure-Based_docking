#!/usr/bin/env python3

#######################################################################
##
##	Peter M.U. Ung @ MSSM
##	
##	v1.0	- 13.11.13
##	v2.0	- 13.11.20 - add FRED tag to filename for future process
##			               FRED score is 1 decimal place
##	v3.0	- 13.11.25 - change to output number based on SDF, not score
##	v4.0	- 13.12.25 - change the read-write organization to use less 
##			               memory
##	v4.5	- 13.12.27 - fixed bug in sdf file reading added functions,
##			               read GZip and BZip2 files
##  v5.0  - 14.05.27 - change Histogram range, allow optional input
##  v6.0  - 16.12.21 - read sdf if name has "::"
##  v6.1  - 17.11.13 - chomp on SDF molname to avoid backspaces
##  v6.2  - 18.02.28 - set default upper/lower for FRED and Glide
##  v7.0  - 18.08.28 - enable SMARTS match to filter out substructures
##  v8.0  - 18.08.29 - rewrite
##  v8.1  - 18.10.30 - bugfix, SMARTS filters for selection and exclusion
##  v9.0  - 19.05.08 - use Seaborn to improve visual of histogram and add
##                     mpi to reading process, but need to watch out mem
##  v10.  - 19.10.15 - fixed a bug with int(args.all_top)*coll > len(d_df)
##
##	Take *_score.txt generated by OpenEye FRED docking to rank molecules.
##	Then read in corresponding SDFs to select ranked molecules for output.
##	Print out the top-ranking sdf molecules and generate a histogram.	
##    -select|-exclude option enables filtering of molecules with matching
##    substructure
##
##	Required:	fred_screen_preprocess.py
##			*.fred_score.txt
##			*.fred_docked.sdf(.bz2|.gz)
##
#######################################################################

import sys
MSG = """\n  ## Usage: x.py 
             [score files: txt] [sdf files: sdf]
             [Number of Top MOL in output: int]
             [docking software: fred | sch | etc]
             [Prefix of Output sdf, png, and txt files]\n
             [optional: -hmax=< default fred:-14.0 | sch:-10.0 >: float]
             [optional: -hmin=< default fred: -2.0 | sch:-3.0  >: float]\n
             [Optional: -coll=< X times top MOL in mem > default: 2x]
             [optional: -exclude=<SMARTS filter> (smt-clean)] removal filter
             [optional: -select=<SMARTS filter>  (smt-selec)] selection filter
             [             use when SMARTS filtering is enabled ]
         ##  TXT and SDF files can also be in GZip/BZip2 format\n
         e.g.: x.py -score "*_score.txt" -sdf "*.sdf" 
                 -top 1000 -dock sch -outpref ksr-allost 
                 -hmax ''-16.0'' -hmin ''-2.0''     # need double quote to work
                 -coll 3
    -exclude 'C(=O)[O-]|S(=O)(=O)[O-]|P(=O)(O)[O-]'  # acidic moieties\n"""
#if len(sys.argv) < 1 or len(sys.argv) > 20: sys.exit(MSG)

import glob,re
import gzip,bz2,gc
import numpy as np
import pandas as pd
import seaborn as sns
import matplotlib.pyplot as plt

from tqdm import tqdm
from rdkit import Chem
from rdkit.Chem import AllChem

from pathos import multiprocessing
from argparse import ArgumentParser
from rdkit_grid_print import grid_print

############################################################################
def doit( ):

    args = UserInput()
    if args.dock == 'fred':
      if args.hmax is None:
        upper = -14.0
      else:
        upper = float(args.hmax)
      if args.hmin is None:
        lower = -2.0
      else:
        lower = float(args.hmin)
    else:
      if args.hmax is None:
        upper = -10.0
      else:
        upper = float(args.hmax)
      if args.hmin is None:
        lower = 0.0
      else:
        lower = float(args.hmin)

    if args.coll is not None:
      coll = int(args.coll)
    else:
      coll = 2
    grid = False
    if args.grid is not None:
      grid = True

    if args.arg_exc is not None:
      arg_exc = args.arg_exc
    else:
      arg_exc = None
    if args.arg_sel is not None:
      arg_sel = args.arg_sel
    else:
      arg_sel = None

######################
    # Read in .fred_score.txt
    File_Names = list(glob.glob(args.all_txt))
    print("Score File: ")
    print(File_Names)

    ## format the output name based on number of top output
    if int(args.all_top) >= 1000:
      top_name = '{0}.{1}_top{2}k'.format( args.prefix, args.dock, int(int(args.all_top)/1000) )
    else:
      top_name = '{0}.{1}_top{2}'.format( args.prefix, args.dock, args.all_top )

    mpi  = multiprocessing.Pool()
    Data = [x for x in tqdm(mpi.imap(ExtractScoreInfo, File_Names), 
                            total=len(File_Names))]
#    Data = [ExtractScoreInfo(fn) for fn in File_Names]
    mpi.close()
    mpi.join()
    d_df = pd.DataFrame(Data[0], columns=['Name','Score']).dropna().sort_values(by=['Score'])

    ## Make histogram of ditribution of FRED scores
    Histogram( d_df.Score, int(args.all_top), top_name, args.dock, upper, lower )
    print("\n  ## Finished plotting overall Top-Ranks ##\n {0} / {1}\n\n".
            format(upper, lower))

##################
    # Read in SDF file name
    SDF_Names = glob.glob(args.all_sdf)
    print("SDF File: ")
    print(SDF_Names)

    ## Build a Top-Selection list, with a 3x head-room for failed molecules 
    ## must pre-select a small subset to avoid collecting too many SDF, which
    ## overload the memory and crash the system
    ## check if the total collected > all data to avoid issue
    if int(args.all_top)*coll > len(d_df):
      num_item = len(d_df) - 1
    else:
      num_item = int(args.all_top)*coll

    print("  ## User-defined output total: "+args.all_top)
    top_df = d_df.loc[:(num_item), ['Name','Score']]

    Top_Hash = top_df.set_index('Name').to_dict()['Score']
    Top_List = (top_df[['Score','Name']]).values  # [ [Score, Name], [], ... ]

####################
    ## Read in top SDF files and build ranked SDF file
    mpi = multiprocessing.Pool(processes=int(multiprocessing.cpu_count()/3))
    sdfdata = CollectSDFData(Top_Hash=Top_Hash)
    Temp = [x for x in tqdm(mpi.imap(sdfdata, SDF_Names), total=len(SDF_Names))]
    mpi.close()
    mpi.join()

    # unpack the list of dict to dict
    Top_sdf = {}
    for d in Temp:
      Top_sdf.update(d)

    if arg_exc is None and arg_sel is None:
      WriteSDFData( Top_List, Top_sdf, int(args.all_top), top_name, args.dock )
    elif arg_exc is None and arg_sel is not None:
      WriteSDFDataSelect(  Top_List, Top_sdf, args.arg_sel, int(args.all_top), top_name, args.dock )
    elif arg_exc is not None and arg_sel is None:
      WriteSDFDataExclude( Top_List, Top_sdf, args.arg_exc, int(args.all_top), top_name, args.dock )
    else:
      sys.exit(' ## ERROR: Only 1 SMARTS filtering should be used ##\n')


#######################################################################
def ExtractScoreInfo( fname ):
  print('file_name: '+str(fname))

  data = pd.read_csv(fname, sep='\s+', comment='#').values.tolist()
#  data = np.genfromtxt(fname, comments=['#','Title', 'Name'], 
#          dtype={'formats': ('S20', np.float16),'names': ('Name', 'Score')})

  print('# Total Ligand Collected: {0}'.format(len(data)))
  return data


##########################################################################
## Build a database of molecules from SDF files
class CollectSDFData(object):
  def __init__( self, Top_Hash='' ):
    self.Top_Hash = Top_Hash

  def __call__( self, sdf_file ):
    return self._read_sdf(sdf_file)

  def _read_sdf( self, sdf_file ):
    ## Build a library of molecules found in the Top-Selction List
    Top_sdf = {}
    print("  # Reading SDF file: "+sdf_file)
    sdf_handle = file_handle(sdf_file)
    Temp_sdf = [x for x in Chem.ForwardSDMolSupplier(sdf_handle,removeHs=False)
                  if x is not None]
    print('  # SDF mol read in from > {0} <: {1}'.format(sdf_file, len(Temp_sdf)))

    ## Rename ligand name if previously processed with '::' tag
    if re.search(r'::', Temp_sdf[0].GetProp('_Name')):
      print('  # Remove "::" tag from ligand name #')
      Temp_sdf = OriginalNameSDF(Temp_sdf)

    prev_name = ''
    for idx, mol in enumerate(Temp_sdf):
      if idx % 10000 == 0: 
        print(' Mol compared {0}'.format(idx))

      ## RDKit may not handle the molecules and make a 'NoneType' item
      ## 'Could not sanitize molecule ending'. Ignore this molecule
      try:
        name = mol.GetProp('_Name')
      except AttributeError:
        print("A molecule failed after this molecule ID: "+prev_name)
        continue
      prev_name = name
      if self.Top_Hash.get(name.rstrip()):
        Top_sdf[name.rstrip()] = mol
    del Temp_sdf    # Free memory
    gc.collect()    # active collection of memory to avoid crash

    return Top_sdf


##########################################################################
def WriteSDFData( Top_List, Top_sdf, all_top, top_name, dock ):

  Select = []
  w   = Chem.SDWriter(top_name+'.sdf')
  OUT = open(top_name+'.txt', 'w')

  ## Use the Ranked list to rebuild a consolidated SDF 
  for idx, Item in enumerate(Top_List):

    score, name = Item[0], Item[1]
    ## If mol_name has conformer number appended on it, remove _NUM
#    if re.search(r'_', name):
#      name = name.split('_')[0]

    if Top_sdf.get(name):
      mol = Top_sdf[name]
    else:
      print(' --> Molecule not found: {0} <--'.format(name))
      continue

    ## Rename mol name property to include data (ZINC, Rank, Score, Software)
    mol.SetProp('_Name', 
        '{0}::{1}::{2:.1f}::{3}'.format(name, idx+1, float(score), dock) )

    OUT.write('{0}\t{1}\n'.format(name, score))
    Select.append(mol)

    ## Close all files when reached the Max. output number
    if len(Select) == all_top:
      for mol in Select:
        w.write(mol) 
      print("\n ## Total Molecule Looked Thru: "+str(idx+1))
      print(' ## Total Molecule Output: '+str(len(Select)))
      OUT.close()
      w.flush()
      w.close()
      gc.collect()
      break

  if grid is True: grid_print(top_name, Select, 'sdf')


#######################################################################
# Remove molecules matching SMARTS strings into .smarts.* files until 
# reaching the targeted number of top-selected molecules
def WriteSDFDataExclude( Top_List, Top_sdf, arg_pat, all_top, top_name, dock ):

  Select, Exclude = [], []
  w   = Chem.SDWriter(top_name+'.smt-clean.sdf')
  OUT = open(top_name+'.smt-clean.txt', 'w')
#  m   = Chem.SDWriter(top_name+'.smt-excl.sdf')
#  SMA = open(top_name+'.smt-excl.txt', 'w')

  ## Use the Ranked list to rebuild a consolidated SDF 
  ## if molecule matches SMARTS filter, separate it 
  for idx, Item in enumerate(Top_List):

    score, name = Item[0], Item[1]
    ## If mol_name has conformer number appended on it, remove _NUM
    if re.search(r'_', name):
      name = name.split('_')[0]

    if Top_sdf.get(name):
      mol    = Top_sdf[name]
      switch = False

      ## Rename mol name property to include data (ZINC, Rank, Score, Software)
      mol.SetProp('_Name',
          '{0}::{1}::{2:.1f}::{3}'.format(name, idx+1, float(score), dock) )

      for smarts in [ p for p in arg_pat.split('|') ]:
        if mol.HasSubstructMatch(Chem.MolFromSmarts(smarts)):
#          print(' ** {0} matches SMARTS {1} - Skip '.format(name, smarts))
          Exclude.append(mol)
#          SMA.write(name+'\t'+str(score)+'\n')
          switch = True
          continue
      if switch:
        continue
    else:
      print(' --> Molecule not found: {0} <--'.format(name))
      continue

    OUT.write(name+"\t"+str(score)+"\n")
    Select.append(mol)

    ## Close all files when reached the Max. output number
    if len(Select) == all_top:
      for mol in Select:
        w.write(mol)
#      for mol in Exclude:
#        m.write(mol)
      print("\n ## Total Molecule Looked Thru: "+str(idx+1))
      print(' ## Molecule Selected: '+str(len(Select)))
      print(' ## Molecule Matched {0}: {1}'.format(arg_exc,len(Exclude)))
      OUT.close()
#      SMA.close()
      w.flush()
      w.close()
#      m.close()
      gc.collect()
      break

  if grid is True: grid_print(top_name, Select, 'sdf')


#######################################################################
# *Select* molecules matching SMARTS strings into .smarts.* files until 
# reaching the targeted number of top-selected molecules
def WriteSDFDataSelect( Top_List, Top_sdf, arg_pat, all_top, top_name, dock ):

  Others, Matched = [], []
  #w   = Chem.SDWriter(top_name+'.smt-filt.sdf')
  #OUT = open(top_name+'.smt-filt.txt', 'w')
  m   = Chem.SDWriter(top_name+'.smt-selec.sdf')
  SMA = open(top_name+'.smt-selec.txt', 'w')

  ## Use the Ranked list to rebuild a consolidated SDF 
  ## if molecule matches SMARTS filter, separate it 
  for idx, Item in enumerate(Top_List):

    score, name = Item[0], Item[1]
    ## If mol_name has conformer number appended on it, remove _NUM
    if re.search(r'_', name):
      name = name.split('_')[0]

    if Top_sdf.get(name):
      mol    = Top_sdf[name]
      switch = False

      ## Rename mol name property to include data (ZINC, Rank, Score, Software)
      mol.SetProp('_Name',
          '{0}::{1}::{2:.1f}::{3}'.format(name, idx+1, float(score), dock) )

      for smarts in [ p for p in arg_pat.split('|') ]:
        if mol.HasSubstructMatch(Chem.MolFromSmarts(smarts)):
          Matched.append(mol)
          SMA.write('{0}\t{1}\n'.format(name, score))
          switch = True
          break
      if not switch:
#        print(' ** {0} not match SMARTS {1} - Skip '.format(name, arg_pat))
        Others.append(mol)
#        OUT.write('{0}\t{1}\n'.format(name, score))
    else:
      print(' --> Molecule not found: {0} <--'.format(name))
      continue

    ## Close all files when reached the Max. output number
    if len(Matched) == all_top or idx == len(Top_List)-1:
#      for mol in Others:
#        w.write(mol)
      for mol in Matched:
        m.write(mol)
      print("\n ## Total Molecule Looked Thru: "+str(idx+1))
      print(' ## Molecule Not Matched: '+str(len(Others)))
      print(' ## Molecule Matched {0}: {1}'.format(arg_sel,len(Matched)))
#      OUT.close()
      SMA.close()
#      w.flush()
#      w.close()
      m.close()
      gc.collect()
      break

  if grid is True: grid_print(top_name, Matched, 'sdf')


#######################################################################
## if "ligand name" has '::' due to previous rdkit processing, remove the
## added data and just the "name" again
def OriginalNameSDF(sdfs):
  NewData = []
  for mol in sdfs:
    name = mol.GetProp('_Name')
    mol.SetProp('_Name', name.split('::')[0])
    NewData.append(mol)
  return NewData


#######################################################################
## Plot Histogram of Score distribution
def Histogram(Histo, top, top_name, dock, UPPER, LOWER ):

    ## if input top number is larger than database size:
    if len(Histo) < top:
      top = len(Histo)

    bin_size  = 0.2
    text_high = 0.275
    text_hori = 0.3

    plt.figure(num=1, figsize=(8,6))

    sns.set_context(  context='notebook', font_scale=1.4)
#          rc={'font.sans-serif': 'Arial', 'font.size':14 })
    
    ## plot histogram - kernel density estimation
    fig = sns.kdeplot(Histo, shade=True, bw='scott')
    sns.despine()

    fig.set_xlabel('Score', fontsize=20 )
    fig.set_ylabel('Fraction of Docked Molecules', fontsize=20 )
    fig.set_title( top_name+": "+str(len(Histo)), fontsize=20 )
    fig.set_xlim([UPPER, LOWER])
    fig.legend().remove()

    ## Draw a vertical line to indicate the Top hits
    fig.axvline( x=Histo[top-1], ymin=0, ymax=1000, 
                color='r', linewidth=3 )
    top_num = 'Top {0}: {1:.2f}'.format(top, Histo[top-1])
    fig.text( Histo[top-1]-text_hori, text_high, 
              top_num, rotation=90, color='black', fontsize=18 )

    ## Draw a vertical line to indicate the Median Score
    fig.axvline( x=np.median(Histo), ymin=0, ymax=1000, 
                color='b', linewidth=3 )
    median = 'Median:{0:.2f}'.format(np.median(Histo))
    fig.text( np.median(Histo)-text_hori, text_high, 
              median, rotation=90, color='black', fontsize=16 )

    ## Draw 2 vertical lines to indicate the standard deviation
    fig.axvline( x=(np.median(Histo)+np.std(Histo)), 
                ymin=0, ymax=1000, color='k', linewidth=1.5 )
    fig.axvline( x=(np.median(Histo)-np.std(Histo)), 
                ymin=0, ymax=1000, color='k', linewidth=1.5 )
    stdev = 'StDev: {0:.2f}'.format(np.std(Histo))
    fig.text( np.median(Histo)+np.std(Histo)-text_hori, text_high, 
              stdev, rotation=90, color='k', fontsize=16 )

#    plt.show()
    fig.figure.savefig( top_name+'.histo.png', dpi=300 )
    plt.close()


#######################################################################
## to handle the raw SDF format, python3's rdkit has a documented bug and
## hasn't been fixed since 2016. https://github.com/rdkit/rdkit/issues/1065
## To avoid it, the input file cannot be an object handle of a regular file,
## i.e. handle = open('xxx.sdf','r') will fail but handle = 'xxx.sdf' is fine.
## It only happens to regular file but not to gzip.open() or bz2.BZ2File() in
## python3 rdkit but not in python2 rdkit...
## Fix it by replace handle = open('xxx.sdf','r') with handle = 'xxx.sdf'

## Handle gzip and bzip2 file if the extension is right. otherwise, just open
## outuput: file handle
def file_handle(file_name):
  if re.search(r'.gz$', file_name):
    handle = gzip.open(file_name, 'rb')
  elif re.search(r'.bz2$', file_name):
    handle = bz2.BZ2File(file_name, 'rb')
  else:
    handle = open(file_name, 'rb')
  return handle

###########################################################################
#### Default boundary constant for Histogram and changes ####
grid= False

def UserInput():
  p = ArgumentParser(description='Command Line Arguments')

  p.add_argument('-score', dest='all_txt', required=True,
                  help='Score Files: txt')
  p.add_argument('-sdf', dest='all_sdf', required=True,
                  help='sdf files: sdf')
  p.add_argument('-top', dest='all_top', required=True,
                  help='Numner of Top mol in output: int')
  p.add_argument('-dock', dest='dock', required=True,
                  help='docking software: fred | sch | etc')
  p.add_argument('-outpref', dest='prefix', required=True,
                  help='Prefix of Output sdf, png, and txt files')

  p.add_argument('-hmax', dest='hmax', required=False,
                  help="optional: -hmax=< default fred:-14.0 | sch:-10.0 >: ''-float''")
  p.add_argument('-hmin', dest='hmin', required=False,
                  help="optional: -hmin=< default fred: -2.0 | sch:-3.0  >: ''-float''")
  p.add_argument('-coll', dest='coll', required=False,
                  help='Optional: -coll=< X times top MOL in mem > default: 2x')
  p.add_argument('-png', dest='grid', required=False,
                  help='?? ignore')

  p.add_argument('-exclude', dest='arg_exc', required=False,
                  help='[Optional: -exclude=<SMARTS filter> (smt-clean)] removal filter')
  p.add_argument('-select', dest='arg_sel', required=False,
                  help='[optional: -select=<SMARTS filter>  (smt-selec)] selection filter\n[             use when SMARTS filtering is enabled ]')

  args = p.parse_args()
  return args

############################################################################
if __name__ == '__main__':
    doit(  )
