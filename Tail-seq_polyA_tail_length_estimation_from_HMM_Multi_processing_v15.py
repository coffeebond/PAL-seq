'''
This script has three modes:
> Mode 1:
1. a text string which defines the prefix name of the output files (-n, required)
2. a text string which specifies the output directory (-d, optional)
3. a text string which indicates what to use for training HMM (-t, optional, default 'all')
4. a reference file with annotation in bed format (-r, required)
5. a STAR-aligned bam file (-b, required)
6. the un-trimmed fastq file from read1 (-f1, required)
7. the fastq file from read2 (-f2, optional, only if 'check_pa_tail' is 'True')
8. intensity file from read2 (-i, required)
9. poly(A) site annotation in bed format (-p, optional)
> Mode 2:
1. a text string which defines the prefix name of the output files (-n, required)
2. a text string which specifies the output directory (-d, optional)
3. a text string which indicates what to use for training HMM (-t, optional, default 'all')
4. T_signal output file generated by this script (-s, required)
> Mode 3:
1. a text string which defines the prefix name of the output files (-n, required)
2. a text string which specifies the output directory (-d, optional)
3. T_signal output file generated by this script (-s, required)
4. HMM model file output from ghmm (-m, required)

Note: When using LSF, make sure to add -n 20 for multiprocessing

It calculates poly(A) tail length based on a Gaussian (mixture) hidden markov model trained on a subset of the data
It outputs these files:
1. all poly(A) tags
2. median poly(A) tail length and total number of tags for each gene
3. mean poly(A) tail length and total number of tags for each gene
4. HMM model 
5. A temporary file containing converted T-signal will be written out on the disk to save memory usage
	and it can be deleted in the end.
6. A temporary file containing the HMM states of each read cycle for each cluster, which can be deleted manually.

v2 change log:
1. use new function 'fread' to read in file flexibly (either txt, gz or tar)
2. only read in two columns (cluster identifier and gene_id) from bedtools output file to make it much faster
3. remove reads that intersect with multiple genes when constructing the dictionary
4. made poly(T) stretch gating more flexible, allowing both Ns and Ts in first 8nts
5. read in intensity file in small chunks to avoid memory failure 

v3 change log:
1. use concurrent.futures for interfacing with multiprocessing
2. added multiprocessing to the section where poly(A) tail length is calculated

v4 change log:
1. output converted T-signals as they are calculated to a file in order to relieve the burden on memory
2. output mean tail-length in addition to median tail-lenght

v6 change log:
1. lowered the criteria for checking if a poly(A) tail exists for a read (5 combined N and T in first 6nts of read 2)

v7 change log:
1. added an imputing step in calculating T-signals, because sometimes there are base positions in read 2 with all channel signals being 0
	the mean of a sliding window is used to fill in these blank positions.
2. changed naming of the output files by adding lane number after the barcodes, in case samples with the same barcode from 
	different lanes are processed in the same folder

v8 change log:
1. added dis2T variable for defining the distance between sequencing starting position and the 3' end of the mRNA.
	This number of bases will be trimmed off when analyzing tail-length
2. added training_min variable that defines the minimal number of clusters for HHM training
3. added r1_len and r2_len variable for defining the length of read 1 and read 2 respectively

v9 change log:
1. added an option for only calculating tail-length if T_signal file is provided. Only one argument is needed for input.
2. changed the mixed gaussian distribution to a simple gaussian distribution in HMM model. 
	The old mixed distribution is preserved in case but not used by default.
3. allowed transition from non-T states back to T states in HMM model
4. tail-length calling by states now requires at least two consecutive non-T states to terminate the T state

v10 change log:
1. signal normalization of each channel now can use all positions with QC score higher than qc_cutoff (default 0) in read1. Note that 
	high QC cutoff may cause biased normalization.
2. added a parameter 'non_T_limit', which allows a certain number of non-T bases at the begining to avoid calling 0-nt tail length
3. modified tail-length calling by states: the distance between the last non-T in the 'non_T_limit' region and 
	the first non-T outside the 'non_T_limit' region

v11 change log:
1. added an option to intersect mapped reads to an additional annotation file as the sixth input argument. This is used when it's 
	preferred to intersect the reads with both genes and poly(A) sites. When two-reference mode is used, the IDs in the first 
	reference will be used to aggregate reads. 
2. changed all 'print' to function 'print', so it is compatible with python 3.

v12 change log:
1. retired filtering bases used for normalization based on QC score due to potential bias and sometime unknown QC score version.
2. Changed read-sampling methods in HMM only mode. Now it's much faster getting the training reads.
3. Added an option ("strand") to specific which strand to use when intersecting with reference files.
4. Added an option ("mixed_model") to perform HMM with either gaussian model (better for PAL-seq) or mixed gaussian model (better for TAIL-seq).
5. Added an option ("allow_back") to initialize HMM transition matrix, either allowing non-T states going back to T-states or not allowing
	non-T states going back to T-states (better for TAIL-seq, slightly better for PAL-seq as well.)

v13 change log (20210307):
1. Moved all global parameters to a dictionary.
2. Changed the input format, and a parser is used.
3. Split the all_tag output file into two files: one with tail length and the other with HMM states, which can be deleted to save space.
4. Added an parameter "check_pa_tail" with the option to filter reads by examining if there is a poly(A) tail

v14 change log (20220525):
1. Changed the region of read1 for normalization, now defined by 'r1_nor_start' and 'r1_nor_end'.
2. Changed the way to filter whether read has a poly(A) tail, now determined by 'len_r2_T_filter' and 'ratio_r2_T_filter'.
3. If the poly(A) tail is checked for read2, a zipped fastq file is written with all reads that don't have a poly(A) tail

v15 change log (20220905; 20221114; 20221217):
1. Added a third mode, which takes in a T-signal file and a HMM model file (trained separately) and predicts tail length directly.
2. Added an option to use fish or human RNA spike as training sets (-t option).
3. Added values of global parameters (Changed to an OrderedDict class) and input arguments to the log output file.

'''


import sys, subprocess, math, numpy, gzip, ghmm, time, tarfile, concurrent.futures, random, os, argparse, shlex
from ghmm import *
from time import time
from datetime import datetime
from collections import OrderedDict

###------global variables-----------------------------------------------------------------
params = OrderedDict([
	('r1_len', 52), # length of read1 (use 40 for v4, and 52 for v5)
	#'r2_len': 255, # length of read2
	('check_pa_tail', False), # whether to use read2 fastq file to filter out reads that don't have a poly(A) tail
	('dis2T', 0), # the number of bases from the sequencing starts in read 2 to the 3' end of the mRNA (use 7 for v4, and 0 for v5)
	('strand', '-'), # positive strand for Tail-seq and negative strand for PAL-seq
	('allow_back', False), # whether to allow HMM to transition from a downstream state back to upstream state
	('mixed_model', True), # whether to use a Gausian mixed model (if not, a simple Gausian model is used)

	('r1_nor_start', 20), # starting position of read1 for signal normalization (use 10 for v4, and 20 for v5)
	('r1_nor_end', 50), # ending position of read1 for signal normalization (use 35 for v4, and 50 for v5)
	#'qc_cutoff': 0, # minimal QC score for a base that is used for signal normalization
	('bound', 5), # boundary for normalized log2 T_signal
	('all_zero_limit', 5), # limit for total number of all zeros in four channels in read 2
	('non_T_limit', 2), # limit for allowing non-T bases at the very 3' ends when calling tail length, due to possible uridylation 
	('len_r2_T_filter', 8), # length of the begining region in read 2 (after 'dist2T') to check poly(T) (when 'check_pa_tail' is 'True')
	('ratio_r2_T_filter', 0.7), # minmal percentage of T in the begining region of read 2 (defined by 'len_r2_T_filter') to check poly(T) (when 'check_pa_tail' is 'True')

	('training_max', 50000), # maximal number of clusters used in the training set
	('training_min', 5000), # minimal number of clusters used in the training set
	('training_ratio', 0.01), # ratio of reads used for training, constrained by "training_max" and "training_min"

	('n_threads', 20),# number of cores to use for multiprocess, if too big, memory may fail
	('chunk_lines', 10000), # number of lines to allocate to each core to process, if too big, memory may fail
	('chunk', 1000000)# give a feedback for proceessing this number of lines
])

t_start = time() # timer start
mdict = {} # master dictionary
dict_tl = {} # dictionary for tail length, using gene_name as key
#qc_code = '@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_`abcdefgh' #QC score coding, default is Illumina 1.5

###------functions------------------------------------------------------------------------
def fread(file): # flexibly read in files for processing
	file = str(file)
	if file.endswith('tar.gz'):
		temp = tarfile.open(file, 'r:gz')
		return temp.extractfile(temp.next())
	elif file.endswith('.gz'):
		return gzip.open(file, 'rb')
	elif file.endswith('.txt'):
		return open(file, 'r')
	else:
		sys.exit("Wrong file type to read: " + file)
	
def Convert2T(line):
# this function does two things:
# 1. normalize intensity for all four channels of each cluster, using part of read1 
	dict_value = {'A':[] ,'C':[] ,'G':[], 'T':[]}
	lst = line.strip('\n').split('\t')
	idx = ':'.join(lst[:3])
	for i in range(params['r1_nor_start'], params['r1_nor_end']+1): # i is the i-th base in read1 
		lst4c = map(int, [x for x in lst[i-1+4].split(' ') if x != '']) 
		# first 4 elements are not intensities
		# get rid of spaces between intensity values
		dict_4c = {'A':lst4c[0] ,'C':lst4c[1] ,'G':lst4c[2], 'T':lst4c[3]}
		base = mdict[idx][1][i-1]
		qc = mdict[idx][2][i-1]
		if dict_value.has_key(base) \
		and dict_4c[base] > 0:
		#and qc_code.index(qc) >= qc_cutoff:
		# QC must be better than qc_cutoff
			dict_value[base].append(dict_4c[base]) 
	for key in dict_value:
		if len(dict_value[key]) == 0:
			return None
			# exit this function if normalization can't be completed
		else:
			dict_value[key] = numpy.mean(dict_value[key])
			# otherwise, take the average value
			# this should be the approximate value illumina used to call the base for that cluster
# 2. convert intensity from 4 channels to T signal
	all_T = []
	for j in range(5 + params['r1_len'] + params['dis2T'], len(lst)+1): # j is j-th position in intensity line
		lst4c = map(int, [x for x in lst[j-1].split(' ') if x != ''])
		if lst4c == [0,0,0,0]:
			all_T.append('empty')
		# sometimes in a base position, all channel signals equal to 0
		# these need to be corrected later or discarded if there are too many in a cluster
		else:
			dict_4c = {'A':lst4c[0] ,'C':lst4c[1] ,'G':lst4c[2], 'T':lst4c[3]}
			for key in dict_4c:
				if dict_4c[key] <= 0:
					dict_4c[key] = 1.0 / dict_value[key]
				else:
					dict_4c[key] = float(dict_4c[key]) / dict_value[key]
				# normalize the intensity value 
			T_signal = dict_4c['T'] / (dict_4c['A'] + dict_4c['C'] + dict_4c['G'])
			T_signal = math.log(T_signal, 2)
			T_signal = max(-params['bound'], min(T_signal, params['bound']))
			# make large or small T_signal bound
			all_T.append(T_signal)
	if all_T.count('empty') >= params['all_zero_limit']:
		return None
	else:
		for k in range(len(all_T)):
			if all_T[k] == 'empty':
				sliding_T = all_T[max(0, k - params['all_zero_limit']):min(len(all_T), k + params['all_zero_limit'])]
				sliding_T = [x for x in sliding_T if x != 'empty']
				all_T[k] = numpy.mean(sliding_T)
		# if there more than all_zero_limit base positions with all channel signals being equal to 0, discard this cluster
		# else, use the mean in a sliding window to fill in the missing value
	all_T.insert(0, params['bound']*100) # add a peudo T to the front, making sure HMM starts with T
	return all_T

def worker_C2T(lines):
# this function takes all read2 intensity lines allocated to each process
# and outputs a tupple including
# 1. a new dictionary which only contains gene_name and converted T-signal
# 2. a list containing converted T-signals to be trained
	temp_dict = {}
	temp_lst = []
	for line in lines:	
		idx = ':'.join(line.strip('\n').split('\t')[:3])
		if mdict.has_key(idx):
			temp = Convert2T(line)
			if temp: # only reads that have converted T-signal will be used later			
				temp_dict.setdefault(idx, [mdict[idx][0],temp])
				if idx in train_keys:
					temp_lst.append(temp)
	return temp_dict, temp_lst

def worker_hmm(lines):
# this function takes a number of ids and calculates the tail length associated
# with each id, and returns a tupple
# 1. a new dictionary which contains gene_name, tail-length and all ghmm states
# 2. a list containing gene_name and tail-length pairs 
	temp_dict = {}
	temp_lst = []
	for line in lines:
		l = line.strip('\n').split('\t')
		states = model.viterbi(EmissionSequence(F, map(float,l[2:])))[0]
		# tail length defined as the distance between two positions:
		# 1. start_idx: the last non-T base within the non_T_limit range 
		# 2. end_idx: the first non-T base outside the non_T_limit range
		# Also, the first position is a peudo-T base
		if len(states) == 0:
			sys.exit("Can't infer the states from the model! Exiting...")
		end_idx = len(states)
		start_idx = 0
		non_A_index_lst = [i for i, x in enumerate(states) if x > ((model.N - 1) / 2)]
		for i in xrange(len(non_A_index_lst)):
			if non_A_index_lst[i] <= params['non_T_limit']:
				start_idx = non_A_index_lst[i]
			else:
				end_idx = non_A_index_lst[i]
				break
		tl = end_idx - start_idx - 1
		temp_dict.setdefault(l[0], [l[1], str(tl), str(states)])
		temp_lst.append([l[1], tl])
	return temp_dict, temp_lst

def lines_sampler(filename, n_lines_sample):
	# this function takes in the reads_wTsignal file and
	# randomly select n_lines_sample lines to output as a list of lists (each containing T signals)
	sample = []
	with fread(filename) as f:
		f.seek(0, 2)
		filesize = f.tell()
		random_set = sorted(random.sample(xrange(filesize), n_lines_sample))
		for i in xrange(n_lines_sample):
			f.seek(random_set[i])
			
			# Skip current line (because we might be in the middle of a line) 
			f.readline()
			
			# Append the next line to the sample set 
			line = f.readline()
			if line:
				sample.append(list(map(float, line.rstrip().split('\t')[2:])))
	return sample
	
def timer(): # calculate runtime
	temp = str(time()-t_start).split('.')[0]
	temp =  '\t' + temp + 's passed...' + '\t' + str(datetime.now())
	return temp

def pwrite(f, text): # a function to simultaneously print texts and record texts in a log file
	f.write(text + '\n')
	print(text)

def make_log_file(filename, p_params = False, p_vars = False):
	f_log = open(filename, 'w')
	if isinstance(p_params, dict):
		pwrite(f_log, 'Global parameters:')
		for param in p_params:
			pwrite(f_log, param + ': ' + str(p_params[param]))
	if isinstance(p_vars, dict):
		pwrite(f_log, '\nInput arguments:')
		for var in p_vars:
			pwrite(f_log, var + ': ' + str(p_vars[var]))
		pwrite(f_log, '\n')	
	return(f_log)

#####################################################################################################################
###------the script runs from there-------------------------------------------------------

# parse the input
parser = argparse.ArgumentParser()
parser.add_argument('-n', '--name', dest = 'n', type = str, help = 'name prefix for output files', required = True)
parser.add_argument('-d', '--directory', dest = 'd', type = str, default = './', help = 'output file directory')
parser.add_argument('-f1', '--fastq_read_1', dest = 'f1', type = str, help = 'input read 1 fastq file')
parser.add_argument('-f2', '--fastq_read_2', dest = 'f2', type = str, help = 'input read 2 fastq file')
parser.add_argument('-b', '--bam', dest = 'b', type = str, help = 'input STAR-aliged bam file')
parser.add_argument('-r', '--ref', dest = 'r', type = str, help = 'input annotation reference file in bed format')
parser.add_argument('-i', '--intensity', dest = 'i', type = str, help = 'input intensity file')
parser.add_argument('-s', '--signal', dest = 's', type = str, help = 'input T signal file')
parser.add_argument('-t', '--train', dest = 't', type = str, default = 'all', help = 'mRNAs as training set')
parser.add_argument('-m', '--model', dest = 'm', type = str, help = 'pre-trained HMM model file (must also provide the T-signal file)')
parser.add_argument('-p', '--pa_site', dest = 'p', type = str, help = 'optional input poly(A) annotation file')
args = parser.parse_args()  

# output file directory and prefix
if not args.d.endswith('/'):
	args.d = args.d + '/'
prefix = args.d + args.n + '_'

###-------------------------------------------------	
if not args.m:
	# if a pre-trained HMM model is not provided, train and predict the data

	if not args.s:
		# mode 1: f T-signal file is not provided, process data to obtain the T-signal file

		if not all([args.f1, args.f2, args.b, args.r, args.i]):
			sys.exit('Missing input files!')

		# read in all files and open files for writing
		ref = str(args.r) # reference file with annotation in bed format
		r1_mapped = str(args.b) # Star aligned bam file
		r1 = str(args.f1) # fastq file from read1 (un-trimmed)
		r2 = str(args.f2) # fastq file from read2
		r2_intensity = str(args.i) # intensity file from read2
		Tsignal_file = prefix +'reads_wTsignal.txt'
		f_log = make_log_file(prefix + 'log.txt', p_params = params, p_vars = vars(args))

		###-------------------------------------------------	
		# intersect mapped read1 to protein-coding genes and
		# construct a dictionary as {unique sequencer-id for each read : [gene name]}
		pwrite(f_log, 'Interecting read1 to protein-coding genes...')
		proc1 = subprocess.check_output(['samtools view ' + r1_mapped + ' | wc -l'], shell=True)
		pwrite(f_log, 'Total number of reads uniquely mapped: ' + str(proc1)) 
		
		if params['strand'] == '+':
			strand_para = '-s'
		else:
			strand_para = '-S'
		proc2 = subprocess.Popen(['bedtools','intersect','-abam',r1_mapped,'-b',ref,'-wa','-wb','-bed', strand_para],stdout=subprocess.PIPE)
		
		if args.p:
			pwrite(f_log, 'Proceeding in two-reference mode...')
			pA_ref = args.p
			proc2p2 = subprocess.Popen(['bedtools','intersect','-a','stdin','-b',pA_ref,'-wa', strand_para],stdin=proc2.stdout, stdout=subprocess.PIPE)
			proc3=subprocess.Popen(['cut','-f4,16'],stdin=proc2p2.stdout, stdout=subprocess.PIPE)
		else:
			pwrite(f_log, 'Proceeding in one-reference mode...')
			proc3 = subprocess.Popen(['cut','-f4,16'],stdin=proc2.stdout, stdout=subprocess.PIPE)
		
		pwrite(f_log, 'Making a master dictionary...' + timer())
		counting = 0
		dict_dup = {} # for storing reads that intersect more than one gene
		while(True):
			line = proc3.stdout.readline()
			if (proc3.poll() is not None) and (not line):
				pwrite(f_log, str(counting) + ' reads processed...' + timer())
				break
			if line:
				lst = line.strip('\n').split('\t')
				idx = ':'.join(lst[0].split('#')[0].split(':')[-3:])
				gene = lst[1]
				if idx in mdict:
					dict_dup[idx] = None
				else:
					mdict[idx] = [gene]
				counting += 1
				if counting%params['chunk'] == 0:
					pwrite(f_log, str(counting) + ' reads processed...' + timer())
		for key in dict_dup:
			del mdict[key]		
		pwrite(f_log, 'Total number of reads mapped to protein-coding genes: ' + str(counting))
		pwrite(f_log, 'Total number of reads uniquely intersect with protein-coding genes: ' + str(len(mdict))) 
		###------------------------------------------------

		###------------------------------------------------	
		# check read2 and remove those that don't have poly(T) sequence
		# need to remove a region that is not part of poly(T)
		# criteria: 5 combined Ts and Ns and As in the first 6nts
		# new dictionary: {unique sequencer-id for each read : [gene name]}
		pwrite(f_log, '\nFiltering the dictionary by examining whether read2 has a poly(A)...' + timer())
		if params['check_pa_tail']:
			counting = 0
			counting_no_tail = 0
			r2 = fread(r2)
			with gzip.open(prefix + 'no_tail_read2.fastq.gz', 'w') as f:
				while(True):
					line1 = r2.readline()
					if not line1:
						pwrite(f_log, str(counting) + ' reads processed...' + timer())
						break
					else:
						line2 = r2.readline()
						line2 = line2[0+params['dis2T']:]
						line2_sub = line2[0+params['dis2T']:]
						line3 = r2.readline()
						line4 = r2.readline()
						idx = ':'.join(line1.split('#')[0].split(':')[-3:])
						if idx in mdict:
							#if (line2[:6].count('T') + line2[:6].count('N') + line2[:6].count('A')) < 5:
							#if line2[:8].count('T') < 7:
							if (line2_sub[:params['len_r2_T_filter']].count('T')) < (params['len_r2_T_filter'] * params['ratio_r2_T_filter']):
								f.write(line1 + line2 + line3 + line4)
								del mdict[idx]
								counting_no_tail += 1
						counting += 1
						if counting%params['chunk'] == 0:
							pwrite(f_log, str(counting) + ' reads processed...' + timer())
			pwrite(f_log, 'Number of reads filtered out due to no poly(A) tail: ' + str(counting_no_tail))
			pwrite(f_log, 'Total number of protein-coding gene-mapped reads that have poly(A) tails: ' + str(len(mdict))) 
			r2.close()
		else:
			pwrite(f_log, '\t' + 'Skipped...' + '\n')
		###------------------------------------------------

		###------------------------------------------------
		# 1. add original read1 sequence to the dictionary item list for normalizing signal
		# 2. pick part of the dictionary as the training set 
		# new dictionary: {unique sequencer-id for each read : [gene name, untrimmed read1 sequence, QC of read1]}
		pwrite(f_log, '\nAdding untrimmed read1 sequence to the filtered dictionary for normalizing signal...')
		pwrite(f_log, 'Spliting training and testing sets...' + timer())
		counting = 0
		counting_dict = 0
		counting_train = 0
		r1 = fread(r1)
		while(True):
			line1 = r1.readline()
			if not line1:
				pwrite(f_log, str(counting) + ' reads processed...' + timer())
				break
			else:
				line2 = r1.readline()
				r1.readline()
				line4 = r1.readline()
				idx = ':'.join(line1.split('#')[0].split(':')[-3:])
				if mdict.has_key(idx):
					mdict[idx].extend([line2.strip('\n'), line4.strip('\n')])
				counting += 1
				if counting%params['chunk'] == 0:
					pwrite(f_log, str(counting) + ' reads processed...' + timer())
		pwrite(f_log, 'The number of reads after accquiring read1 sequence: ' + str(len(mdict))) 

		# choose the set of reads as training
		if args.t in ['fish', 'human']: # select mRNAs, use 10 fold training ratio  
			pwrite(f_log, 'Randomly picking training set from ' + args.t + ' mRNA reads:')
			if args.t == 'fish':
				sele_dict = {x:mdict[x] for x in mdict if mdict[x][0][:4] == 'ENSD'}
			elif args.t == 'human':
				sele_dict = {x:mdict[x] for x in mdict if (mdict[x][0][:6] == 'pA_chr') or ('ENSG' in mdict[x][0])}
			pwrite(f_log, 'The total number of ' + args.t + ' mRNA reads:' + str(len(sele_dict)))
			if len(sele_dict) >= params['training_min']:
				n_train_lines = min(max(int(len(sele_dict)*params['training_ratio']*10), params['training_min']), params['training_max'])
				train_keys = random.sample(list(sele_dict.keys()), n_train_lines)
				pwrite(f_log, str(n_train_lines) + ' ' + args.t + ' mRNA reads picked for training...')
			else:
				args.t = 'all'
				pwrite(f_log, 'Not enough ' + args.t + ' mRNA reads for training. Use all mRNA reads for picking training set...')
		if args.t == 'all': # all mRNAs
			pwrite(f_log, 'Randomly picking a training set from all reads:')
			if len(mdict) >= params['training_min']:
				n_train_lines = min(max(int(len(mdict)*params['training_ratio']), params['training_min']), params['training_max'])
				train_keys = random.sample(list(mdict.keys()), n_train_lines)
				pwrite(f_log, str(n_train_lines) + ' mRNA reads picked for training...')
			else:
				pwrite(f_log, 'Not enough reads for training. Exiting...' + timer())
				sys.exit()
		r1.close()
		###------------------------------------------------

		###------------------------------------------------
		# read intensity file and convert 4-channel intensities to single log-transformed bound T_signal
		# output T_signal to a file
		pwrite(f_log, '\nReading read2 intensity file...' + timer())	
		counting = 0
		rounds = 0
		counting_sum = 0
		counting_out = 0
		chunk_temp = params['chunk']
		train_set = []
		line_lst = []
		r2_intensity = fread(r2_intensity)
		output_Tsignal = open(Tsignal_file, 'w')
		while(1):
			line = r2_intensity.readline()
			if not line:
				with concurrent.futures.ProcessPoolExecutor(params['n_threads']) as pool:
					futures = pool.map(worker_C2T,[line_lst[n:n+params['chunk_lines']] for n in xrange(0,len(line_lst),params['chunk_lines'])])
					for (d,l) in futures: # combine data from outputs from all processes
						train_set.extend(l)
						for key in d: # write converted T-signal to a file
							output_Tsignal.write(key+'\t'+d[key][0]+'\t'+'\t'.join(map(str,d[key][1]))+'\n')
							counting_out += 1
					counting_sum += counting
					pwrite(f_log, str(counting_sum) + ' reads processed...' + timer())
				break
			else:
				line_lst.append(line)
				counting += 1
				if counting % (params['chunk_lines'] * params['n_threads']) == 0:
					rounds += 1
					with concurrent.futures.ProcessPoolExecutor(params['n_threads']) as pool:
						futures = pool.map(worker_C2T,[line_lst[n:n+params['chunk_lines']] for n in xrange(0,len(line_lst),params['chunk_lines'])])
						for (d,l) in futures: # combine data from outputs from all processes
							train_set.extend(l)
							for key in d: # write converted T-signal to a file
								output_Tsignal.write(key+'\t'+d[key][0]+'\t'+'\t'.join(map(str,d[key][1]))+'\n')
								counting_out += 1
						counting_sum = counting * rounds
						if counting_sum > chunk_temp:
							chunk_temp += params['chunk']
							pwrite(f_log, str(counting_sum) + ' reads processed...' + timer())
					line_lst = []
					counting = 0
		pwrite(f_log, 'The number of reads in training set after intensity-conversion: ' + str(len(train_set)))
		pwrite(f_log, 'Total number of reads after intensity-conversion: ' + str(counting_out))
		pwrite(f_log, 'Finished processing read2 intensity file...' + timer())
		r2_intensity.close()
		output_Tsignal.close()
		mdict.clear() # clear the dictionary to free up some memory
		###------------------------------------------------

	###------------------------------------------------
	else:
		# mode 2: 
		# if a T-signal file is provided, train and predict with provided T-signal data

		f_log = make_log_file(prefix + 'hmm_only_log.txt', p_params = params, p_vars = vars(args))
		pwrite(f_log, 'Starting HMM mode with provided T-signal file...' + timer())
		if not os.path.isfile(args.s):
			sys.exit('Error! No Tsignal file found!')
		else:
			Tsignal_file = args.s

		# determine the set of mRNAs used for training
		if args.t in ['fish', 'human']:
			pwrite(f_log, 'Use ' + args.t + ' mRNA spike-in as training set...' + timer())
			pwrite(f_log, 'Obtain ' + args.t + ' mRNA T signals and write them in a new file...')
			sele_Tsignal_file = prefix + args.t + '_mRNA_reads_wTsignal.txt'
			with open(sele_Tsignal_file, 'w') as f:
				if args.t == 'fish':
					command = 'awk \'$2 ~ \"^ENSD\"\' ' + Tsignal_file
				elif args.t == 'human':
					command = 'awk \'$2 ~ \"^pA_chr[XYM0-9]\" || $2 ~ \"ENSG\"\' ' + Tsignal_file
				proc = subprocess.Popen(shlex.split(command), stdout=f).communicate()
			
			# estimate the number of lines by dividing the total file size by the size of first line
			Tsignal_input = fread(sele_Tsignal_file)
			Tsignal_input.readline()
			line_size = int(Tsignal_input.tell())
			Tsignal_input.seek(0,2)
			file_size = int(Tsignal_input.tell())
			if line_size != 0 and file_size / line_size > params['training_min']:
				T_lines = file_size / line_size
				params['training_ratio'] = 0.1
			else:
				pwrite(f_log, 'Not enough ' + args.t + ' mRNA reads for training. Use all mRNA reads for picking training set...')
				args.t = 'all'
		if args.t == 'all':
			pwrite(f_log, 'Use all mRNA spike-in as training set...' + timer())
			Tsignal_input = fread(Tsignal_file)
			Tsignal_input.readline()
			line_size = int(Tsignal_input.tell())
			Tsignal_input.seek(0,2)
			file_size = int(Tsignal_input.tell())
			T_lines = file_size / line_size

		pwrite(f_log, 'Estimated total number of reads eligible for used as training: ' + str(T_lines))
		pwrite(f_log, 'Randomly picking eligible reads as training set:')
		n_train_lines = min(max(int(T_lines*params['training_ratio']), params['training_min']), params['training_max'])
		pwrite(f_log, str(n_train_lines) + ' reads picked for training...' + timer())
		Tsignal_input.close()
		if args.t in ['fish', 'human']:
			train_set = lines_sampler(sele_Tsignal_file, n_train_lines)
		else:
			train_set = lines_sampler(Tsignal_file, n_train_lines)
		

	###------------------------------------------------
	# initializes a gaussian hidden markov model and defines
	# the tranisition, emission, and starting probabilities
	print('\nTraining data with hmm...' + timer())
	F = ghmm.Float()

	pi = [1.0, 0.0, 0.0, 0.0, 0.0] # initial state

	if params['allow_back'] == True:
		# The following matrix allows T states going back to non=T states.
		Transitionmatrix = [[0.04, 0.93, 0.02, 0.01, 0.0],
							[0.0, 0.87, 0.1, 0.02, 0.01],
	         	           [0.0, 0.05, 0.6, 0.3, 0.05],
	         	           [0.0, 0.01, 0.3, 0.6, 0.09],
	         	           [0.0, 0.01, 0.01, 0.1, 0.88]]
	else:
		# The following matrix does not allow states going backwards.
		Transitionmatrix = [[0.04, 0.93, 0.02, 0.01, 0.0],
							[0.0, 0.94, 0.03, 0.02, 0.01],
		                    [0.0, 0.0, 0.5, 0.4, 0.1],
		                    [0.0, 0.0, 0.0, 0.6, 0.4],
		                    [0.0, 0.0, 0.0, 0.0, 1.0]]
	# state 0: peudo-T state
	# state 1: definitive-T state
	# state 2: likely-T state
	# state 3: likely-non-T state
	# state 4: definitive-non-T state

	if params['mixed_model'] == True:
		Emissionmatrix = [[[params['bound']*100.0, 0.0], [1.0, 1.0], [1.0, 0.0]],
					  [[1.5, -1.0 ], [1.5, 1.5], [0.95, 0.05]],
	                  [[1.5, -1.0 ], [1.5, 1.5], [0.75, 0.25]],
	                  [[1.5, -1.0 ], [1.5, 1.5], [0.5, 0.5]],
	                  [[1.5, -1.0 ], [1.5, 1.5], [0.25, 0.75]]]
		# [p1_mean, p2,mean], [p1_std, p2_std], [P(p1), P(p2)]
		model = ghmm.HMMFromMatrices(F, ghmm.GaussianMixtureDistribution(F), Transitionmatrix, Emissionmatrix, pi)
	else:
		Emissionmatrix = [[params['bound']*100.0, 1.0],
						  [2.0, 0.5],
		                  [1.0, 0.5],
		                  [-1.0, 0.5],
		                  [-2.0, 0.5]]
		# [mean, std]
		model = ghmm.HMMFromMatrices(F, ghmm.GaussianDistribution(F), Transitionmatrix, Emissionmatrix, pi)

	print('Model before training:')
	print(model)
	mghmm_train = ghmm.SequenceSet(F, train_set)
	model.baumWelch(mghmm_train, 10000, 0.01)
	print('Model after training:')
	print(model)
	out_hmm = prefix + 'HMM_model.txt' # HMM model
	model.write(out_hmm)

else:
	# mode 3: 
	# if a pre-trained model is provided, load the model and predict with the model
	F = ghmm.Float()
	model = ghmm.HMMOpenXML(args.m)
	f_log = make_log_file(prefix + 'prediction_only_log.txt', p_params = params, p_vars = vars(args))
	pwrite(f_log, '\nA pre-trained HMM model is provided. No training is carried out. Starting prediction...')
	print(model)
	out_hmm = prefix + 'HMM_model.txt' # HMM model
	model.write(out_hmm)

	if not os.path.isfile(args.s):
		sys.exit('Error! No Tsignal file found!')
	else:
		Tsignal_file = args.s

###------------------------------------------------


###------------------------------------------------
# output files
output_all = open(prefix + 'all_tails.txt', 'w') # tail lengths of all tags
output_states = open(prefix + 'hmm_states.txt', 'w') # HMM states of all tags
output_median = open(prefix + 'median_tails_tags.txt', 'w') # median tail lengths, aggregated by genes
output_mean = open(prefix + 'mean_tails_tags.txt', 'w') # mean tail lengths, aggregated by genes

###------------------------------------------------
# calculate tail length using the mghmm model and write them to output files
# dict_tl structure: {gene_name : [list of tail lengths]}
pwrite(f_log, '\nCalculating tail-lengths and writing outputs...' + timer())
lst_tl = [] # for storing gene_name, tail-length pairs
counting = 0
rounds = 0
counting_sum = 0
counting_out = 0
chunk_temp = params['chunk']
Tsignal_input = fread(Tsignal_file)
line_lst = []
while(1):
	line = Tsignal_input.readline()
	if not line:
		with concurrent.futures.ProcessPoolExecutor(params['n_threads']) as pool:
			futures = pool.map(worker_hmm,[line_lst[n:n+params['chunk_lines']] for n in xrange(0,len(line_lst),params['chunk_lines'])])
			for (d, l) in futures: # combine data from outputs from all processes
				lst_tl.extend(l)
				for key in d: # write single tail tags to the output file
					output_all.write(d[key][0] + '\t' + key + '\t' + d[key][1] + '\n')
					output_states.write(key + '\t' + d[key][2] + '\n')
					counting_out += 1
			counting_sum += counting
			pwrite(f_log, str(counting_sum) + ' reads processed...' + timer())
			break
	else:
		line_lst.append(line)
		counting += 1
		if counting % (params['chunk_lines'] * params['n_threads']) == 0:
			rounds += 1
			with concurrent.futures.ProcessPoolExecutor(params['n_threads']) as pool:
				futures = pool.map(worker_hmm,[line_lst[n:n+params['chunk_lines']] for n in xrange(0,len(line_lst),params['chunk_lines'])])
				for (d, l) in futures: # combine data from outputs from all processes
					lst_tl.extend(l)
					for key in d: # write single tail tags to the output file
						output_all.write(d[key][0] + '\t' + key + '\t' + d[key][1] + '\n')
						output_states.write(key + '\t' + d[key][2] + '\n')
						counting_out += 1
				counting_sum = counting * rounds
				if counting_sum > chunk_temp:
					chunk_temp += params['chunk']
					pwrite(f_log, str(counting_sum) + ' reads processed...' + timer()) 
			line_lst = []
			counting = 0
pwrite(f_log, 'Total number of tail-lengths written: ' + str(counting_out))
pwrite(f_log, 'Finished calculating tail lengths...' + timer())
Tsignal_input.close()
output_all.close()
output_states.close()

# delete temporary file containing converted T-signal (very big)
#subprocess.call(['rm','-f',Tsignal_file])

for pair in lst_tl: # transform data for calculating median and mean tail length
	if dict_tl.has_key(pair[0]):
		dict_tl[pair[0]].append(pair[1])
	else:
		dict_tl.setdefault(pair[0],[pair[1]])
for key in dict_tl:
	output_median.write(key + '\t' + str(numpy.median(dict_tl[key])) + '\t' + str(len(dict_tl[key])) + '\n')		
for key in dict_tl:
	output_mean.write(key + '\t' + str(numpy.mean(dict_tl[key])) + '\t' + str(len(dict_tl[key])) + '\n')			
pwrite(f_log, 'Total number of genes with tail-length written: ' + str(len(dict_tl)))

output_median.close()
output_mean.close()
pwrite(f_log, 'Final: ' + timer())		
















		
		
		
		
		
