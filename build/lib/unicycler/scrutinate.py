#!/usr/bin/env python3
'''
Scrutinate - a long read assembly checker

Author: Ryan Wick
email: rrwick@gmail.com
'''

import sys
import re
import imp
import os
import string
import argparse
from .misc import int_to_str, float_to_str, check_file_exists, quit_with_error, check_graphmap, \
                  get_nice_header, reverse_complement, print_progress_line, print_section_header
from .read_ref import load_references, load_long_reads
from .alignment import AlignmentScoringScheme
from .cpp_function_wrappers import simulate_depths, get_random_sequence_alignment_error_rates
from .antpath import semi_global_align_long_reads, add_aligning_arguments, fix_up_arguments

VERBOSITY = 0 # Controls how much the script prints to the screen
CONSOLE_WIDTH = 40 # The width of many things printed to stdout

def main():
    '''
    Script execution starts here.
    '''
    args = get_arguments()
    must_perform_alignment = not os.path.isfile(args.sam)
    full_command = ' '.join(sys.argv)    
    check_file_exists(args.ref)
    check_file_exists(args.reads)
    if must_perform_alignment and not args.no_graphmap:
        check_graphmap(args.graphmap_path)

    references = load_references(args.ref, VERBOSITY)
    reference_dict = {x.name: x for x in references}
    read_dict, read_names = load_long_reads(args.reads, VERBOSITY)

    if must_perform_alignment:
        scoring_scheme = AlignmentScoringScheme(args.scores)
    else:
        scoring_scheme = get_scoring_scheme_from_sam(args.sam)

    if must_perform_alignment:
        semi_global_align_long_reads(references, args.ref, read_dict, read_names, args.reads,
                                     args.temp_dir, args.graphmap_path, args.threads,
                                     scoring_scheme, args.low_score, not args.no_graphmap,
                                     args.keep_bad, args.kmer, args.min_len, args.sam,
                                     full_command, VERBOSITY)

    alignments = load_sam_alignments(args.sam, read_dict, reference_dict, scoring_scheme,
                                     args.threads)

    count_depth_and_errors_per_base(references, reference_dict, alignments)
    high_error_rate, very_high_error_rate, random_seq_error_rate, mean_error_rate = \
                    determine_thresholds(scoring_scheme, references, alignments,
                                         args.threads, args.depth_p_val, args.error_rate_threshold)
    count_depth_and_errors_per_window(references, args.error_window_size, args.depth_window_size,
                                      high_error_rate, very_high_error_rate)

    if args.window_tables:
        window_tables_prefix = prepare_output_dirs(args.window_tables)
        produce_window_tables(references, window_tables_prefix)

    if args.base_tables:
        base_tables_prefix = prepare_output_dirs(args.base_tables)
        produce_base_tables(references, base_tables_prefix)

    if args.html:
        produce_html_report(references, args.html, high_error_rate, very_high_error_rate,
                            random_seq_error_rate, full_command, args.ref, args.sam,
                            scoring_scheme, alignments, mean_error_rate,
                            args.error_window_size, args.depth_window_size,
                            args.depth_p_val, args.error_rate_threshold)

    if VERBOSITY > 0:
        produce_console_output(references)

    sys.exit(0)

def get_arguments():
    '''
    Specifies the command line arguments required by the script.
    '''
    parser = argparse.ArgumentParser(description='Long read assembly checker',
                                     formatter_class=argparse.ArgumentDefaultsHelpFormatter)

    parser.add_argument('--sam', type=str, required=True, default=argparse.SUPPRESS,
                        help='Input SAM file of alignments (if this file doesn\'t exist, the '
                             'alignment will be performed with results saved to this file - you '
                             'can use the aligner arguments with this script)')
    parser.add_argument('--ref', type=str, required=True, default=argparse.SUPPRESS,
                        help='FASTA file containing one or more reference sequences')
    parser.add_argument('--reads', type=str, required=True, default=argparse.SUPPRESS,
                        help='FASTQ file of long reads')
    parser.add_argument('--error_window_size', type=int, required=False, default=100,
                        help='Window size for error summaries')
    parser.add_argument('--depth_window_size', type=int, required=False, default=100,
                        help='Window size for depth summaries')
    parser.add_argument('--error_rate_threshold', type=float, required=False, default=0.3,
                        help='Threshold for high error rates, expressed as the fraction between '
                             'the mean error rate and the random alignment error rate')
    parser.add_argument('--depth_p_val', type=float, required=False, default=0.01,
                        help='P-value for low/high depth thresholds')
    parser.add_argument('--window_tables', type=str, required=False, default=argparse.SUPPRESS,
                        help='Path and/or prefix for table files summarising reference errors for '
                             'reference windows (default: do not save window tables)')
    parser.add_argument('--base_tables', type=str, required=False, default=argparse.SUPPRESS,
                        help='Path and/or prefix for table files summarising reference errors at '
                             'each base (default: do not save base tables)')
    parser.add_argument('--html', type=str, required=False, default=argparse.SUPPRESS,
                        help='Path for HTML report (default: do not save HTML report)')
    parser.add_argument('--threads', type=int, required=False, default=argparse.SUPPRESS,
                        help='Number of CPU threads used to align (default: the number of '
                             'available CPUs)')
    parser.add_argument('--verbosity', type=int, required=False, default=1,
                        help='Level of stdout information (0 to 2)')

    # Add the arguments for the aligner, but suppress the help text.
    add_aligning_arguments(parser, True)

    args = parser.parse_args()
    fix_up_arguments(args)

    global VERBOSITY
    VERBOSITY = args.verbosity

    # If some arguments weren't set, set them to None/False. We don't use None/False as a default
    # in add_argument because it makes the help text look weird.
    try:
        args.window_tables
    except AttributeError:
        args.window_tables = None
    try:
        args.base_tables
    except AttributeError:
        args.base_tables = None
    try:
        args.html
    except AttributeError:
        args.html = None

    if args.depth_p_val > 0.1 or args.depth_p_val <= 0.0:
        quit_with_error('--depth_p_val must be greater than 0.0 and less than or equal to 0.1')
    if args.error_rate_threshold >= 1.0 or args.error_rate_threshold <= 0.0:
        quit_with_error('--error_rate_threshold must be greater than 0.0 and less than 1.0')

    if args.html:
        check_plotly_exists()

    # Add the process ID to the default temp directory so multiple instances can run at once in the
    # same directory.
    args.temp_dir = args.temp_dir.replace('PID', str(os.getpid()))

    return args

def prepare_output_dirs(output_prefix):
    '''
    Ensures the output prefix is nicely formatted and any necessary directories are made.
    '''
    if output_prefix is None:
        return None
    if os.path.isdir(output_prefix) and not output_prefix.endswith('/'):
        output_prefix += '/'
    if output_prefix.endswith('/') and not os.path.isdir(output_prefix):
        os.makedirs(output_prefix)
    if not output_prefix.endswith('/'):
        directory = os.path.dirname(output_prefix)
        if directory and not os.path.isdir(directory):
            os.makedirs(directory)
    return output_prefix

def check_plotly_exists():
    '''
    Checks to see if the plotly library is available. If so, it's imported. If not, quit with an
    error.
    '''
    try:
        imp.find_module('plotly')
    except ImportError:
        quit_with_error('plotly not found - please install plotly package to produce html plots')

def get_scoring_scheme_from_sam(sam_filename):
    '''
    Looks for the 'SC' tag in the SAM file to get the alignment scoring scheme.
    '''
    sam_file = open(sam_filename, 'r')
    for line in sam_file:
        line = line.strip()

        # If we've reached the end of the header and still not found the scoring scheme, just
        # return a simple generic one.
        if not line.startswith('@'):
            return AlignmentScoringScheme('1,-1,-1,-1')

        line_parts = line.split('\t')
        for part in line_parts:
            if part.startswith('SC:'):
                scoring_scheme_string = part[3:]
                if scoring_scheme_string.count(',') == 3:
                    return AlignmentScoringScheme(scoring_scheme_string)

    return AlignmentScoringScheme('1,-1,-1,-1')

def get_random_sequence_error_rate(scoring_scheme):
    '''
    Returns the expected number of errors per reference base for an alignment of random sequences
    using the given scoring scheme.
    '''
    # I've precalculated the error rate for some typical scoring schemes.
    scoring_scheme_str = str(scoring_scheme)
    if scoring_scheme_str == '1,0,0,0':
        return 0.498197
    elif scoring_scheme_str == '0,-1,-1,-1':
        return 0.506547
    elif scoring_scheme_str == '1,-1,-1,-1':
        return 0.489942
    elif scoring_scheme_str == '5,-4,-8,-6': # GraphMap
        return 0.496997
    elif scoring_scheme_str == '5,-6,-10,0': # BLASR
        return 0.585428
    elif scoring_scheme_str == '2,-5,-2,-1': # BWA-MEM
        return 0.52616
    elif scoring_scheme_str == '1,-3,-5,-2': # CUSHAW2 / blastn-short
        return 0.482431
    elif scoring_scheme_str == '5,-11,-2,-4': # proovread
        return 0.571232
    elif scoring_scheme_str == '3,-6,-5,-2': # my aligner
        return 0.477499
    elif scoring_scheme_str == '2,-3,-5,-2': # blastn / dc-megablast
        return 0.471655
    elif scoring_scheme_str == '1,-2,0,0': # megablast
        return 0.571199
    elif scoring_scheme_str == '0,-6,-5,-3': # Bowtie2 end-to-end
        return 0.466259
    elif scoring_scheme_str == '2,-6,-5,-3': # Bowtie2 local
        return 0.468592
    elif scoring_scheme_str == '1,-4,-6,-1': # BWA
        return 0.523119

    # If the scoring scheme doesn't match a previously known one, we will use the C++ code to get
    # an error rate estimate.
    else:
        error_rate_str = get_random_sequence_alignment_error_rates(1000, 100, scoring_scheme)
        return float(error_rate_str.split('\n')[1].split('\t')[8])

def load_sam_alignments(sam_filename, read_dict, reference_dict, scoring_scheme, threads):
    '''
    This function returns a list of Alignment objects from the given SAM file.
    '''
    print_section_header('Loading alignments', VERBOSITY)

    # Load the SAM lines into a list.
    sam_lines = []
    sam_file = open(sam_filename, 'r')
    for line in sam_file:
        line = line.strip()
        if line and not line.startswith('@') and line.split('\t', 3)[2] != '*':
            sam_lines.append(line)
    num_alignments = sum(1 for line in open(sam_filename) if not line.startswith('@'))
    if not num_alignments:
        quit_with_error('there are no alignments in the file: ' + sam_filename)
    if VERBOSITY > 0:
        print_progress_line(0, num_alignments)

    # If single-threaded, just do the work in a simple loop.
    threads = 1 # TEMP
    sam_alignments = []
    if threads == 1:
        for line in sam_lines:
            sam_alignments.append(Alignment(line, read_dict, reference_dict, scoring_scheme))
            if VERBOSITY > 0:
                print_progress_line(len(sam_alignments), num_alignments)

    # # If multi-threaded, use processes.
    # else:
    #     sam_line_groups = chunkify(sam_lines, threads)
    #     manager = Manager()
    #     workers = []
    #     sam_alignments = manager.list([])
    #     for sam_line_group in sam_line_groups:
    #         child = Process(target=make_alignments, args=(sam_line_group, read_dict,
    #                                                       reference_dict, scoring_scheme,
    #                                                       sam_alignments))
    #         child.start()
    #         workers.append(child)
    #     while any(i.is_alive() for i in workers):
    #         time.sleep(0.1)
    #         if VERBOSITY > 0:
    #             print_progress_line(len(sam_alignments), num_alignments)
    #     for worker in workers:
    #         worker.join()
    #     sam_alignments = sam_alignments._getvalue()

    # At this point, we should have loaded num_alignments alignments. But check to make sure and
    # fix up the progress line if any didn't load.
    if VERBOSITY > 0:
        if len(sam_alignments) < num_alignments:
            print_progress_line(len(sam_alignments), len(sam_alignments))
        print()

    return sam_alignments

# def chunkify(full_list, pieces):
#     '''
#     http://stackoverflow.com/questions/2130016/
#     splitting-a-list-of-arbitrary-size-into-only-roughly-n-equal-parts
#     '''
#     return [full_list[i::pieces] for i in range(pieces)]

# def make_alignments(sam_lines, read_dict, reference_dict, scoring_scheme, alignments):
#     '''
#     Produces alignments from SAM lines and deposits them in a managed list.
#     '''
#     for line in sam_lines:
#         alignments.append(Alignment(line, read_dict, reference_dict, scoring_scheme))

def count_depth_and_errors_per_base(references, reference_dict, alignments):
    '''
    Counts up the depth and errors for each base of each reference and stores the counts in the
    Reference objects.
    '''
    print_section_header('Counting depth and errors', VERBOSITY)
    if VERBOSITY > 0:
        print_progress_line(0, len(alignments))

    for ref in references:
        ref_length = ref.get_length()
        ref.depths = [0] * ref_length
        ref.mismatch_counts = [0] * ref_length
        ref.insertion_counts = [0] * ref_length
        ref.deletion_counts = [0] * ref_length
        ref.error_rates = [None] * ref_length
        ref.alignment_count = 0

    for i, alignment in enumerate(alignments):
        ref = reference_dict[alignment.ref.name]
        ref.alignment_count += 1
        for j in range(alignment.ref_start_pos, alignment.ref_end_pos):
            ref.depths[j] += 1
            if ref.error_rates[j] is None:
                ref.error_rates[j] = 0.0
        for j in alignment.ref_mismatch_positions:
            ref.mismatch_counts[j] += 1
        for j in alignment.ref_insertion_positions:
            ref.insertion_counts[j] += 1
        for j in alignment.ref_deletion_positions:
            ref.deletion_counts[j] += 1
        if VERBOSITY > 0:
            print_progress_line(i+1, len(alignments))

    if VERBOSITY > 0:
        print()
        base_sum = sum([x.get_length() for x in references])
        finished_bases = 0
        print_section_header('Totalling depth and errors', VERBOSITY)
        print_progress_line(finished_bases, base_sum)

    for ref in references:
        ref_length = ref.get_length()
        for i in range(ref_length):
            if ref.depths[i] > 0:
                error_count = ref.mismatch_counts[i] + ref.insertion_counts[i] + \
                              ref.deletion_counts[i]
                ref.error_rates[i] = error_count / ref.depths[i]
            if VERBOSITY > 0:
                finished_bases += 1
                if finished_bases % 10 == 0:
                    print_progress_line(finished_bases, base_sum)

    if VERBOSITY > 0:
        print_progress_line(base_sum, base_sum)
        print()


def count_depth_and_errors_per_window(references, er_window_size, depth_window_size,
                                      high_error_rate, very_high_error_rate):
    '''
    Counts up the depth and errors for each window of each reference and stores the counts in the
    Reference objects.
    '''
    for ref in references:
        ref_length = ref.get_length()

        er_window_count = max(1, int(round(ref_length / er_window_size)))
        ref.er_window_size = ref_length / er_window_count
        ref.er_window_starts = []
        ref.window_error_rates = []
        ref.high_error_regions = []
        current_high_error_region = None

        for i in range(er_window_count):
            window_start = int(round(ref.er_window_size * i))
            window_end = int(round(ref.er_window_size * (i + 1)))
            ref.er_window_starts.append(window_start)
            this_window_size = window_end - window_start
            this_window_pos_with_error_rate = 0

            total_window_error_rate = None
            for j in range(window_start, window_end):
                if ref.error_rates[j] is not None:
                    this_window_pos_with_error_rate += 1
                    if total_window_error_rate is None:
                        total_window_error_rate = 0.0
                    total_window_error_rate += ref.error_rates[j]

            # Check for high error regions.
            if total_window_error_rate is None:
                window_error_rate = None
            else:
                window_error_rate = total_window_error_rate / this_window_pos_with_error_rate
                if window_error_rate > very_high_error_rate:
                    if current_high_error_region is None:
                        current_high_error_region = (window_start, window_end)
                    else:
                        current_high_error_region = (current_high_error_region[0], window_end)
                elif window_error_rate < high_error_rate: # error rate is not high
                    if current_high_error_region is not None:
                        ref.high_error_regions.append(current_high_error_region)
                        current_high_error_region = None

            ref.window_error_rates.append(window_error_rate)

        if current_high_error_region is not None:
            ref.high_error_regions.append(current_high_error_region)

        depth_window_count = max(1, int(round(ref_length / depth_window_size)))
        ref.depth_window_size = ref_length / depth_window_count
        ref.depth_window_starts = []
        ref.window_depths = []
        ref.low_depth_regions = []
        ref.high_depth_regions = []
        current_low_depth_region = None
        current_high_depth_region = None

        for i in range(depth_window_count):
            window_start = int(round(ref.depth_window_size * i))
            window_end = int(round(ref.depth_window_size * (i + 1)))
            ref.depth_window_starts.append(window_start)
            this_window_size = window_end - window_start
            total_window_depth = 0
            for j in range(window_start, window_end):
                total_window_depth += ref.depths[j]

            # Check for low depth regions.
            window_depth = total_window_depth / this_window_size
            if window_depth < ref.very_low_depth_cutoff:
                if current_low_depth_region is None:
                    current_low_depth_region = (window_start, window_end)
                else:
                    current_low_depth_region = (current_low_depth_region[0], window_end)
            elif window_depth > ref.low_depth_cutoff: # depth is not low
                if current_low_depth_region is not None:
                    ref.low_depth_regions.append(current_low_depth_region)
                    current_low_depth_region = None

            # Check for high depth regions.
            if window_depth > ref.very_high_depth_cutoff:
                if current_low_depth_region is None:
                    current_high_depth_region = (window_start, window_end)
                else:
                    current_high_depth_region = (current_high_depth_region[0], window_end)
            elif window_depth < ref.high_depth_cutoff: # depth is not high
                if current_high_depth_region is not None:
                    ref.high_depth_regions.append(current_high_depth_region)
                    current_high_depth_region = None

            ref.window_depths.append(window_depth)

        if current_low_depth_region is not None:
            ref.low_depth_regions.append(current_low_depth_region)
        if current_high_depth_region is not None:
            ref.high_depth_regions.append(current_high_depth_region)

        # Calculate the min/max/mean window depth and error rate for this reference.
        ref.min_window_depth = 0.0
        ref.max_window_depth = 0.0
        ref.mean_window_depth = 0.0
        if ref.window_depths:
            ref.min_window_depth = min(ref.window_depths)
            ref.max_window_depth = max(ref.window_depths)
            ref.mean_window_depth = sum(ref.window_depths) / len(ref.window_depths)
        ref.min_window_error_rate = None
        ref.max_window_error_rate = None
        ref.mean_window_error_rate = None
        not_none_error_rates = [x for x in ref.window_error_rates if x is not None]
        if not_none_error_rates:
            ref.min_window_error_rate = min(not_none_error_rates)
            ref.max_window_error_rate = max(not_none_error_rates)
            ref.mean_window_error_rate = sum(not_none_error_rates) / len(not_none_error_rates)

def determine_thresholds(scoring_scheme, references, alignments, threads, depth_p_val,
                         error_rate_fraction):
    '''
    This function sets thresholds for error rate and depth. Error rate thresholds are set once for
    all references, while depth thresholds are per-reference.
    '''
    print_section_header('Setting error and depth thresholds', VERBOSITY)

    # Find the mean of all error rates.
    all_error_rates = []
    for ref in references:
        all_error_rates += [x for x in ref.error_rates if x is not None]
    mean_error_rate = get_mean(all_error_rates)
    if VERBOSITY > 0:
        print(lr_justify('Mean error rate:',
                         float_to_str(mean_error_rate * 100.0, 2) + '%'))
    random_seq_error_rate = get_random_sequence_error_rate(scoring_scheme)
    if VERBOSITY > 0:
        print(lr_justify('Random alignment error rate:',
                         float_to_str(random_seq_error_rate * 100.0, 2) + '%'))
        print()

    # The mean error rate should not be as big as the random alignment error rate.
    if mean_error_rate >= random_seq_error_rate:
        quit_with_error('the mean error rate (' + float_to_str(mean_error_rate * 100.0, 2) + \
                        '%) is too high and exceeds the randoml alignment error rate (' + \
                        float_to_str(random_seq_error_rate * 100.0, 2) + '%)')
    
    difference = random_seq_error_rate - mean_error_rate
    high_error_rate = mean_error_rate + (error_rate_fraction * 0.5 * difference)
    very_high_error_rate = mean_error_rate + (error_rate_fraction * difference)

    if VERBOSITY > 0:
        print(lr_justify('Error rate threshold 1:',
                         float_to_str(high_error_rate * 100.0, 2) + '%'))
        print(lr_justify('Error rate threshold 2:',
                         float_to_str(very_high_error_rate * 100.0, 2) + '%'))
        print()

    for ref in references:
        determine_depth_thresholds(ref, alignments, threads, 0.1, depth_p_val)

    return high_error_rate, very_high_error_rate, random_seq_error_rate, mean_error_rate


def determine_depth_thresholds(ref, alignments, threads, depth_p_val_1, depth_p_val_2):
    '''
    This function determines read depth thresholds by simulating a random distribution of reads.
    '''
    alignment_lengths = [x.ref_end_pos - x.ref_start_pos for x in alignments \
                         if x.ref.name == ref.name]
    ref_length = ref.get_length()

    min_depth_dist, max_depth_dist = get_depth_min_and_max_distributions(alignment_lengths,
                                                                         ref_length, 10000,
                                                                         threads)
    ref.low_depth_cutoff = get_low_depth_cutoff(min_depth_dist, depth_p_val_1)
    ref.very_low_depth_cutoff = get_low_depth_cutoff(min_depth_dist, depth_p_val_2)

    ref.high_depth_cutoff = get_high_depth_cutoff(max_depth_dist, depth_p_val_1)
    ref.very_high_depth_cutoff = get_high_depth_cutoff(max_depth_dist, depth_p_val_2)

    if VERBOSITY > 0:
        print(ref.name + ':')
        print(lr_justify('   low depth threshold: ', int_to_str(ref.very_low_depth_cutoff)))
        print(lr_justify('   high depth threshold:', int_to_str(ref.very_high_depth_cutoff)))
        print()


def get_low_depth_cutoff(min_depth_dist, p_val):
    dist_sum = 0.0
    for depth, fraction in reversed(min_depth_dist):
        dist_sum += fraction
        if dist_sum >= 1.0 - p_val:
            return depth
    return 0

def get_high_depth_cutoff(max_depth_dist, p_val):
    dist_sum = 0.0
    for depth, fraction in max_depth_dist:
        dist_sum += fraction
        if dist_sum >= 1.0 - p_val:
            return depth
    return max_depth_dist[-1][0]

def depths_to_capture_fraction(depth_distribution, fraction):
    '''
    Returns a min and max depth which capture at least the given fraction of the distribution.
    '''
    depths = list(depth_distribution.keys())
    proportions = list(depth_distribution.values())
    mode_depth = depths[proportions.index(max(proportions))]
    low_cutoff = mode_depth
    high_cutoff = mode_depth
    fraction_captured = depth_distribution[mode_depth]
    while fraction_captured < fraction:
        next_possible_low = low_cutoff - 1
        next_possible_high = high_cutoff + 1
        fraction_from_next_low = 0.0
        fraction_from_next_high = 0.0
        if next_possible_low in depth_distribution:
            fraction_from_next_low = depth_distribution[next_possible_low]
        if next_possible_high in depth_distribution:
            fraction_from_next_high = depth_distribution[next_possible_high]
        if fraction_from_next_low > fraction_from_next_high:
            low_cutoff = next_possible_low
            fraction_captured += fraction_from_next_low
        elif fraction_from_next_high > fraction_from_next_low:
            high_cutoff = next_possible_high
            fraction_captured += fraction_from_next_high
        elif fraction_from_next_high > 0.0: # they are equal but non-zero.
            high_cutoff = next_possible_high
            fraction_captured += fraction_from_next_high
        else: # they are both zero.
            if next_possible_low >= 0:
                low_cutoff = next_possible_low
            high_cutoff = next_possible_high
    return low_cutoff, high_cutoff

def get_mean(num_list):
    '''
    This function returns the mean of the given list of numbers.
    '''
    if not num_list:
        return None
    return sum(num_list) / len(num_list)

def produce_console_output(references):
    '''
    Write a summary of the results to std out.
    '''
    for ref in references:
        print()
        print('Results: ' + ref.name)
        print('-' * max(CONSOLE_WIDTH, len(ref.name) + 9))
        ref_length = ref.get_length()

        print(lr_justify('Length:', int_to_str(ref_length) + ' bp'))
        print(lr_justify('Alignments:', int_to_str(ref.alignment_count))) 
        print()
        min_er = ref.min_window_error_rate
        print(lr_justify('Min error rate:', 'n/a') if min_er is None else \
              lr_justify('Min error rate:', (float_to_str(min_er * 100.0, 1) + '%')))
        mean_er = ref.mean_window_error_rate
        print(lr_justify('Mean error rate:', 'n/a') if mean_er is None else \
              lr_justify('Mean error rate:', (float_to_str(mean_er * 100.0, 1) + '%')))
        max_er = ref.max_window_error_rate
        print(lr_justify('Max error rate:', 'n/a') if max_er is None else \
              lr_justify('Max error rate:', (float_to_str(max_er * 100.0, 1) + '%')))
        print()

        if ref.high_error_regions:
            print('High error regions:')
            for i, high_error_region in enumerate(ref.high_error_regions):
                print('  ' + str(i+1) + ') ' + int_to_str(high_error_region[0]) +
                      ' bp to ' + int_to_str(high_error_region[1]) + ' bp')
        else:
            print(lr_justify('High error regions:', 'none'))
        print()

        print(lr_justify('Min depth:', float_to_str(ref.min_window_depth, 1) + 'x'))
        print(lr_justify('Mean depth:', float_to_str(ref.mean_window_depth, 1) + 'x'))
        print(lr_justify('Max depth:', float_to_str(ref.max_window_depth, 1) + 'x'))
        print()

        if ref.low_depth_regions:
            print('Low depth regions:')
            for i, low_depth_region in enumerate(ref.low_depth_regions):
                print(str(i+1) + ') ' + int_to_str(low_depth_region[0]) +
                      ' bp to ' + int_to_str(low_depth_region[1]) + ' bp')
            print()
        else:
            print(lr_justify('Low depth regions:', 'none'))

        if ref.high_depth_regions:
            print('High depth regions:')
            for i, high_depth_region in enumerate(ref.high_depth_regions):
                print('   ' + str(i+1) + ') ' + int_to_str(high_depth_region[0]) + ' bp to ' + 
                      int_to_str(high_depth_region[1]) + ' bp')
        else:
            print(lr_justify('High depth regions:', 'none'))
        print()

def lr_justify(str_1, str_2):
    '''
    Concatenates the two strings with enough spaces in the middle to make the
    whole string at least CONSOLE_WIDTH in width.
    '''
    return str_1 + (' ' * (CONSOLE_WIDTH - len(str_1) - len(str_2))) + str_2

def clean_str_for_filename(filename):
    '''
    This function removes characters from a string which would not be suitable in a filename.
    It also turns spaces into underscores, because filenames with spaces can occasionally cause
    issues.
    http://stackoverflow.com/questions/295135/turn-a-string-into-a-valid-filename-in-python
    '''
    valid_chars = "-_.() %s%s" % (string.ascii_letters, string.digits)
    filename_valid_chars = ''.join(c for c in filename if c in valid_chars)
    return filename_valid_chars.replace(' ', '_')

def add_ref_name_to_output_prefix(ref, output_prefix, ending):
    clean_ref_name = clean_str_for_filename(ref.name)
    if output_prefix.endswith('/'):
        return output_prefix + clean_ref_name + ending
    else:
        return output_prefix + '_' + clean_ref_name + ending

def produce_window_tables(references, window_tables_prefix):
    '''
    Write tables of depth and error rates per reference window.
    '''
    print_section_header('Saving window tables', VERBOSITY)

    for ref in references:
        window_table_filename = add_ref_name_to_output_prefix(ref, window_tables_prefix, '.txt')
        table = open(window_table_filename, 'w')
        table.write('\t'.join(['Window start',
                               'Window end',
                               'Mean depth',
                               'Mean error rate']) + '\n')
        window_count = len(ref.window_starts)
        for i in range(window_count):
            if i + 1 == window_count:
                window_end = ref.get_length()
            else:
                window_end = ref.window_starts[i+1]
            table.write('\t'.join([str(ref.window_starts[i]),
                                   str(window_end),
                                   str(ref.window_depths[i]),
                                   str(ref.window_error_rates[i])]) + '\n')
        if VERBOSITY > 0:
            print(window_table_filename)

def produce_base_tables(references, base_tables_prefix):
    '''
    Write tables of depth and error counts per reference base.
    '''
    print_section_header('Saving base tables', VERBOSITY)
    for ref in references:
        base_table_filename = add_ref_name_to_output_prefix(ref, base_tables_prefix, '.txt')
        table = open(base_table_filename, 'w')
        table.write('\t'.join(['Base',
                               'Read depth',
                               'Mismatches',
                               'Deletions',
                               'Insertions']) + '\n')
        for i in range(ref.get_length()):
            table.write('\t'.join([str(i+1),
                                   str(ref.depths[i]),
                                   str(ref.mismatch_counts[i]),
                                   str(ref.deletion_counts[i]),
                                   str(ref.insertion_counts[i])]) + '\n')
        if VERBOSITY > 0:
            print(base_table_filename)

def produce_html_report(references, html_filename, high_error_rate, very_high_error_rate,
                        random_seq_error_rate, full_command, ref_filename, sam_filename,
                        scoring_scheme, alignments, mean_error_rate, er_window_size,
                        depth_window_size, depth_p_val, error_rate_fraction):
    '''
    Write html files containing plots of results.
    '''
    print_section_header('Saving html plots', VERBOSITY)
    import plotly.offline as py
    import plotly.graph_objs as go

    if not html_filename.endswith('.htm') and not html_filename.endswith('.html'):
        html_filename += '.html'

    report_width = 1000

    html_file = open(html_filename, 'w')
    html_file.write(get_html_start(report_width))

    # Add a title and general information to the report.
    html_file.write('<h1>Long read assembly checker</h1>\n')
    html_file.write('<h5>Hold the mouse over text in this report for explanations.</h5>\n')
    html_file.write(get_report_html_table(ref_filename, sam_filename, full_command, os.getcwd(),
                                          scoring_scheme, references, alignments,
                                          random_seq_error_rate, very_high_error_rate,
                                          mean_error_rate, er_window_size, depth_window_size,
                                          error_rate_fraction))
    first_reference = True
    for ref in references:
        html_file.write('<br><br><br>\n')
        ref_name_help = 'This is the name of the reference sequence, taken from the reference ' + \
                        'FASTA file.'
        html_file.write('<h2 title="' + ref_name_help + '">' + ref.name + '</h2>\n<hr>\n')
        html_file.write(get_reference_html_table(ref))
        error_rate_help = 'Error rates are defined as the fraction of alignment errors ' + \
                          '(mismatches, insertions and deletions) for all reads at a given ' + \
                          'base in the reference. Insertions only count as a single error, ' + \
                          'regardless of their length. If a region of the reference has ' + \
                          'abnormally high error rates (often appearing as a sharp spike), ' + \
                          'that could indicate misassembly.'
        html_file.write('<h3 title="' + error_rate_help + '">Error rate</h3>')
        html_file.write(get_error_rate_plotly_plot(ref, py, go, first_reference,
                                                   high_error_rate, very_high_error_rate,
                                                   random_seq_error_rate, report_width))
        html_file.write(get_reference_error_rate_html_table(ref, er_window_size))
        read_depth_help = 'Read depths are defined as the number of alignments which span a ' + \
                          'given base of the reference. If a region of the reference has ' + \
                          'abnormally low or high error rates (often appearing as a shift to ' + \
                          'a different mean), that could indicate misassembly. However, there ' + \
                          'are other possible reasons for read depth variation, so regions of ' + \
                          'high or low depth do not always indicate misassembly.'
        html_file.write('<h3 title="' + read_depth_help + '">Read depth</h3>')
        html_file.write(get_depth_plotly_plot(ref, py, go, False, report_width))
        html_file.write(get_reference_depth_html_table(ref, depth_window_size, depth_p_val))

        # Note that the first reference is done. This is so we only save the Plotly Javascript to
        # the HTML once.
        first_reference = False

    # Finish the HTML file.
    html_file.write(get_html_end())
    html_file.close()

    if VERBOSITY > 0:
        print(os.path.abspath(html_filename))


def get_error_rate_plotly_plot(ref, py, go, include_javascript, high_error_rate,
                               very_high_error_rate, random_seq_error_rate, report_width):
    '''
    Returns the HTML div for the error rate plot.
    '''
    half_er_window_size = ref.er_window_size / 2
    x = []
    y = []
    for i, window_start in enumerate(ref.er_window_starts):
        x.append(window_start + half_er_window_size)
        if ref.window_error_rates[i] is None:
            y.append(None)
        else:
            y.append(round(100.0 * ref.window_error_rates[i], 2))
    if all(y_val is None for y_val in y):
        return ''

    max_error_rate = max(y)

    # Prepare the points.
    error_trace = go.Scatter(x=x, y=y, mode='lines',
                             line=dict(color='rgb(50, 50, 50)', width=2))
    data = [error_trace]

    # Produce the coloured background rectangles.
    red, yellow, green = get_plot_background_colours()
    max_x = ref.get_length()
    max_y = max(max_error_rate * 1.05, 100.0 * random_seq_error_rate)
    error_rate_background = [dict(type='rect', x0=0, y0=0,
                                  x1=max_x, y1=high_error_rate * 100.0,
                                  line=dict(width=0), fillcolor=green),
                             dict(type='rect', x0=0, y0=high_error_rate * 100.0,
                                  x1=max_x, y1=very_high_error_rate * 100.0,
                                  line=dict(width=0), fillcolor=yellow),
                             dict(type='rect', x0=0, y0=very_high_error_rate * 100.0,
                                  x1=max_x, y1=max_y,
                                  line=dict(width=0), fillcolor=red)]

    layout = dict(autosize=False, width=report_width-15, height=300, hovermode='closest',
                  margin=go.Margin(l=40, r=10, b=10, t=10),
                  xaxis=dict(range=[0, max_x], rangeslider=dict(), type='linear'),
                  yaxis=dict(ticksuffix='%', range=[0.0, max_y]), shapes=error_rate_background)

    fig = dict(data=data, layout=layout)
    return '<div style="align:center"><div class="plotbox">' + \
           py.plot(fig, output_type='div', include_plotlyjs=include_javascript, show_link=False) + \
           '</div></div>'

def get_depth_plotly_plot(ref, py, go, include_javascript, report_width):
    '''
    Returns the HTML div for the error rate plot.
    '''
    half_depth_window_size = ref.depth_window_size / 2
    x = []
    y = []
    for i, window_start in enumerate(ref.depth_window_starts):
        x.append(window_start + half_depth_window_size)
        y.append(ref.window_depths[i])
    if all(y_val is None for y_val in y):
        return ''
    max_depth = max(y)

    # Prepare the points.
    depth_trace = go.Scatter(x=x, y=y, mode='lines',
                             line=dict(color='rgb(50, 50, 50)', width=2))
    data = [depth_trace]

    # Produce the coloured background rectangles.
    red, yellow, green = get_plot_background_colours()
    max_x = ref.get_length()
    max_y = max(max_depth * 1.05, ref.very_high_depth_cutoff * 1.2)
    depth_background = []
    current_y = 0
    if ref.very_low_depth_cutoff > 0:
        depth_background.append(dict(type='rect', x0=0, y0=current_y,
                                     x1=max_x, y1=ref.very_low_depth_cutoff,
                                     line=dict(width=0), fillcolor=red))
        current_y = ref.very_low_depth_cutoff
    if ref.low_depth_cutoff > 0:
        depth_background.append(dict(type='rect', x0=0, y0=current_y,
                                     x1=max_x, y1=ref.low_depth_cutoff,
                                     line=dict(width=0), fillcolor=yellow))
        current_y = ref.low_depth_cutoff
    depth_background.append(dict(type='rect', x0=0, y0=current_y,
                                 x1=max_x, y1=ref.high_depth_cutoff,
                                 line=dict(width=0), fillcolor=green))
    depth_background.append(dict(type='rect', x0=0, y0=ref.high_depth_cutoff,
                                 x1=max_x, y1=ref.very_high_depth_cutoff,
                                 line=dict(width=0), fillcolor=yellow))
    depth_background.append(dict(type='rect', x0=0, y0=ref.very_high_depth_cutoff,
                                 x1=max_x, y1=max_y, line=dict(width=0), fillcolor=red))

    # Create the depth plot.
    layout = dict(autosize=False, width=report_width-10, height=300, hovermode='closest',
                  margin=go.Margin(l=40, r=10, b=10, t=10),
                  xaxis=dict(range=[0, max_x], rangeslider=dict(), type='linear'),
                  yaxis=dict(ticksuffix='x', range=[0.0, max_y]), shapes=depth_background)

    fig = dict(data=data, layout=layout)
    return '<div style="align:center"><div class="plotbox">' + \
           py.plot(fig, output_type='div', include_plotlyjs=include_javascript, show_link=False) + \
           '</div></div>'

def get_plot_background_colours():
    '''
    Returns strings describing the red, yellow and green (in that order) used for the plot
    backgrounds.
    '''
    red = 'rgba(255, 0, 0, 0.1)'
    yellow = 'rgba(255, 200, 0, 0.1)'
    green = 'rgba(50, 200, 50, 0.1)'
    return red, yellow, green

def get_html_start(report_width):
    return '<!DOCTYPE html>\n<html>\n' + \
           get_html_style(report_width) + \
           '<body><div class="content">\n'

def get_html_end():
    return '</div></body>\n</html>\n'

def get_html_style(report_width):
    style = '<style>\n'
    style += 'body {' + \
             'text-align: center;' + \
             'position: relative; ' + \
             'font-family: verdana, arial, helvetica, sans-serif; ' + \
             'color: #323232; ' + \
             'background-color: #666;' + \
             '}\n'
    style += 'div.content {' + \
             'width: ' + str(report_width) + 'px; ' + \
             'padding: 15px; ' + \
             'background: #F0F0F0; ' + \
             'margin-top: 15px; ' + \
             'margin-bottom: 10px; ' + \
             'margin-right: auto; ' + \
             'margin-left: auto; ' + \
             'border: 2px solid #323232; ' + \
             'text-align:left; ' + \
             '}\n'
    style += 'div.plotbox {' + \
             'background-color:#ffffff; ' + \
             'width: ' + str(report_width - 5) + 'px; ' + \
             'border: 2px solid #323232; ' + \
             '}\n'
    style += 'h2 {word-wrap: break-word;}\n'
    # style += 'h3 {text-align: center;}\n'
    style += 'table {' + \
             'padding: 10px; ' + \
             'border-collapse: collapse; ' + \
             'border-style: hidden; ' + \
             '}\n'
    style += 'td {' + \
             'padding: 5px; ' + \
             'border-bottom: 1px solid #d5d5d5; ' + \
             'word-wrap: break-word; ' + \
             '}\n'
    style += 'td.monospace {' + \
             'font-family: \'Lucida Console\', monospace; ' + \
             '}\n'
    style += '</style>\n'
    return style

def get_report_html_table(ref_filename, sam_filename, full_command, directory, scoring_scheme,
                          references, alignments, random_seq_error_rate, very_high_error_rate,
                          mean_error_rate, er_window_size, depth_window_size, error_rate_fraction):
    '''
    Produces the table of information at the top of the report, not specific to any one reference.
    '''
    table = '<table width="100%">\n'
    table += '  <col width="30%">\n'

    ref_file_help = 'The file of reference sequences used in the alignment.'
    table += '  <tr title="' + ref_file_help + '">' + \
             '<td>Reference file:</td>' + \
             '<td class="monospace">' + ref_filename + \
             '</td></tr>\n'

    ref_count_help = 'The number of separate sequences in the reference file.'
    table += '  <tr title="' + ref_count_help + '">' + \
             '<td>Number of references:</td>' + \
             '<td>' + int_to_str(len(references)) + \
             '</td></tr>\n'

    total_ref_length = sum([x.get_length() for x in references])
    total_ref_length_help = 'The sum of the length of all reference sequences.'
    table += '  <tr title="' + total_ref_length_help + '">' + \
             '<td>Total reference length:</td>' + \
             '<td>' + int_to_str(total_ref_length) + ' bp' + \
             '</td></tr>\n'

    ref_file_help = 'The SAM file of alignments. These alignments are assumed to be ' + \
                    'semi-global (i.e. not clipped except at the end of a reference).'
    table += '  <tr title="' + ref_file_help + '">' + \
             '<td>Alignment file:</td>' + \
             '<td class="monospace">' + sam_filename + \
             '</td></tr>\n'

    total_alignments_help = 'The number of alignments in the SAM file.'
    table += '  <tr title="' + total_alignments_help + '">' + \
             '<td>Total alignments:</td>' + \
             '<td>' + int_to_str(len(alignments)) + \
             '</td></tr>\n'

    full_command_help = 'The command used to generate this HTML report.'
    full_command = full_command.replace(' --', ' &#x2011&#x2011')
    table += '  <tr title="' + full_command_help + '">' + \
             '<td>Full command:</td>' + \
             '<td class="monospace">' + full_command + \
             '</td></tr>\n'

    directory_help = 'The directory where the above command was executed.'
    table += '  <tr title="' + directory_help + '">' + \
             '<td>Directory of execution:</td>' + \
             '<td class="monospace">' + directory + '</td></tr>\n'

    scoring_scheme_help = 'The scores used in the alignment (gotten from the SAM file).'
    table += '  <tr title="' + scoring_scheme_help + '">' + \
             '<td>Scoring scheme:</td>' + \
             '<td>' + scoring_scheme.get_full_string() + \
             '</td></tr>\n'

    mean_error_rate_help = 'The average error rate across all references.'
    table += '  <tr title="' + mean_error_rate_help + '">' + \
             '<td>Mean error rate:</td>' + \
             '<td>' + float_to_str(mean_error_rate * 100.0, 1) + '%' + \
             '</td></tr>\n'

    random_seq_error_rate_help = 'The average error rate for random sequences (using the ' + \
                                 'specified scoring scheme). This is the expected error rate ' + \
                                 'when two sequences do not align.'
    table += '  <tr title="' + random_seq_error_rate_help + '">' + \
             '<td>Random alignment error rate:</td>' + \
             '<td>' + float_to_str(random_seq_error_rate * 100.0, 1) + '%' + \
             '</td></tr>\n'

    very_high_error_rate_help = int_to_str(er_window_size) + ' bp windows exceeding this ' + \
                                'threshold are counted as high error regions. This is ' + \
                                'indicated in the plots with the colour red. ' + \
                                'This value was set to an intermediate value (' + \
                                float_to_str(error_rate_fraction * 100.0, 1) + '%) ' + \
                                'between the mean error rate and random alignment error rate.'
    table += '  <tr title="' + very_high_error_rate_help + '">' + \
             '<td>High error threshold:</td>' + \
             '<td>' + float_to_str(very_high_error_rate * 100.0, 1) + '%' + \
             '</td></tr>\n'

    er_window_size_help = 'The size of the sliding window used to make the error rate plots. ' + \
                          'I.e. the plotted error rates are averages over a reference window ' + \
                          'of this size.'
    table += '  <tr title="' + er_window_size_help + '">' + \
             '<td>Error rate window size:</td>' + \
             '<td>' + int_to_str(er_window_size) + ' bp' + \
             '</td></tr>\n'

    depth_window_size_help = 'The size of the sliding window used to make the depth plots. ' + \
                          'I.e. the plotted read depths are averages over a reference window ' + \
                          'of this size.'
    table += '  <tr title="' + depth_window_size_help + '">' + \
             '<td>Read depth window size:</td>' + \
             '<td>' + int_to_str(depth_window_size) + ' bp' + \
             '</td></tr>\n'

    total_high_error_regions = sum([len(x.high_error_regions) for x in references])
    total_high_error_regions_help = 'The total count of high error regions for all ' + \
                                    'references. A value of 0 indicates a good assembly.'
    table += '  <tr title="' + total_high_error_regions_help + '">' + \
             '<td>Total high error regions:</td>' + \
             '<td>' + int_to_str(total_high_error_regions) + \
             '</td></tr>\n'

    total_depth_problem_regions = sum([len(x.low_depth_regions) + len(x.high_depth_regions) \
                                       for x in references])
    total_depth_problems_help = 'The total count of depth problem regions for all ' + \
                                'references. A value of 0 is ideal, but depth problems do not ' + \
                                'necessarily indicate misassembly.'
    table += '  <tr title="' + total_depth_problems_help + '">' + \
             '<td>Total depth problem regions:</td>' + \
             '<td>' + int_to_str(total_depth_problem_regions) + \
             '</td></tr>\n'

    table += '</table>\n'
    return table

def get_reference_html_table(ref):
    table = '<table width="100%">\n'
    table += '  <col width="30%">\n'

    ref_length_help = 'The length of this reference sequence.'
    table += '  <tr title="' + ref_length_help + '">' + \
             '<td>Length:</td>' + \
             '<td>' + int_to_str(ref.get_length()) + ' bp' + \
             '</td></tr>\n'

    alignment_count_help = 'The number of alignments for this reference.'
    table += '  <tr title="' + alignment_count_help + '">' + \
             '<td>Alignments:</td>' + \
             '<td>' + int_to_str(ref.alignment_count) + \
             '</td></tr>\n'

    table += '</table>\n'
    return table

def get_reference_error_rate_html_table(ref, er_window_size):
    '''
    Produces an HTML table summarising the error rate for the reference.
    '''
    window_str = int_to_str(er_window_size) + ' bp'
    table = '<br>\n'
    table += '<table width="100%">\n'
    table += '  <col width="30%">\n'

    min_error_rate_help = 'The lowest error rate of any ' + window_str + ' window in this ' + \
                          'reference.'
    table += '  <tr title="' + min_error_rate_help + '">' + \
             '<td>Min error rate:</td><td>'
    table += 'n/a' if ref.min_window_error_rate is None \
                   else float_to_str(ref.min_window_error_rate * 100.0, 1)
    table += '%</td></tr>\n'

    mean_error_rate_help = 'The mean error rate for all ' + window_str + ' windows in this ' + \
                           'reference.'
    table += '  <tr title="' + mean_error_rate_help + '">' + \
             '<td>Mean error rate:</td><td>'
    table += 'n/a' if ref.mean_window_error_rate is None \
                   else float_to_str(ref.mean_window_error_rate * 100.0, 1)
    table += '%</td></tr>\n'

    max_error_rate_help = 'The highest error rate of any ' + window_str + ' window in this ' + \
                          'reference.'
    table += '  <tr title="' + max_error_rate_help + '">' + \
             '<td>Max error rate:</td><td>'
    table += 'n/a' if ref.max_window_error_rate is None \
                   else float_to_str(ref.max_window_error_rate * 100.0, 1)
    table += '%</td></tr>\n'

    table += '</table>\n'

    table += '<br>\n'
    table += '<table width="100%">\n'
    if ref.high_error_regions:
        high_error_regions_help = 'These regions of the reference exceed the high error rate ' + \
                                  'threshold.'
        table += '  <tr title="' + high_error_regions_help + '">' + \
                 '<th>High error regions</th></tr>'
        for i, high_error_region in enumerate(ref.high_error_regions):
            table += '      <tr title="' + high_error_regions_help + '">' + \
                     '<td>' + int_to_str(i+1) + ')  ' + \
                     int_to_str(high_error_region[0]) + ' bp to ' + \
                     int_to_str(high_error_region[1]) + ' bp</td></tr>\n'
    else:
        no_high_error_regions_help = 'No ' + window_str + ' windows in this reference exceed the high error rate ' + \
                                     'threshold.'
        table += '  <tr title="' + no_high_error_regions_help + '">' + \
                 '<th>No high error regions</th></tr>'
    table += '</table>\n'

    return table

def get_reference_depth_html_table(ref, depth_window_size, depth_p_val):
    '''
    Produces an HTML table summarising the read depth for the reference.
    '''
    window_str = int_to_str(depth_window_size) + ' bp'
    table = '<br>\n'
    table += '<table width="100%">\n'
    table += '  <col width="30%">\n'

    min_depth_help = 'The lowest error rate of any ' + window_str + ' window in this reference.'
    table += '  <tr title="' + min_depth_help + '">' + \
             '<td>Min depth:</td><td>' + \
             float_to_str(ref.min_window_depth, 1) + 'x' + \
             '</td></tr>\n'

    mean_depth_help = 'The mean depth for all ' + window_str + ' windows in this reference.'
    table += '  <tr title="' + mean_depth_help + '">' + \
             '<td>Mean depth:</td><td>' + \
             float_to_str(ref.mean_window_depth, 1) + 'x' + \
             '</td></tr>\n'

    max_depth_help = 'The highest error rate of any ' + window_str + ' window in this reference.'
    table += '  <tr title="' + max_depth_help + '">' + \
             '<td>Max depth:</td><td>' + \
             float_to_str(ref.max_window_depth, 1) + 'x' + \
             '</td></tr>\n'

    low_depth_help = window_str + ' windows with depth below this threshold are counted as ' + \
                     'low depth regions. This is indicated in the plots with the colour red. ' + \
                     'This value was set using the p-value of ' + float_to_str(depth_p_val, 3) + \
                     '. When reads are placed randomly, only ' + \
                     float_to_str(depth_p_val * 100.0, 1) + '% of trials contain a lower ' + \
                     'minimum depth.'
    table += '  <tr title="' + low_depth_help + '">' + \
             '<td>Low depth threshold:</td><td>' + \
             float_to_str(ref.very_low_depth_cutoff, 1) + 'x' + \
             '</td></tr>\n'

    high_depth_help = window_str + ' windows with depth above this threshold are counted as ' + \
                      'high depth regions. This is indicated in the plots with the colour red. ' + \
                     'This value was set using the p-value of ' + float_to_str(depth_p_val, 3) + \
                     '. When reads are placed randomly, only ' + \
                     float_to_str(depth_p_val * 100.0, 1) + '% of trials contain a higher ' + \
                     'maximum depth.'
    table += '  <tr title="' + high_depth_help + '">' + \
             '<td>High depth threshold:</td><td>' + \
             float_to_str(ref.very_high_depth_cutoff, 1) + 'x' + \
             '</td></tr>\n'

    table += '</table>\n'

    table += '<br>\n'
    table += '<table width="100%">\n'
    if ref.low_depth_regions:
        low_depth_regions_help = 'These regions of the reference fall below the low depth ' + \
                                 'threshold.'
        table += '  <tr title="' + low_depth_regions_help + '">' + \
                 '<th>Low depth regions</th></tr>'
        for i, low_depth_region in enumerate(ref.low_depth_regions):
            table += '      <tr title="' + low_depth_regions_help + '">' + \
                     '<td>' + int_to_str(i+1) + ')  ' + \
                     int_to_str(low_depth_region[0]) + ' bp to ' + \
                     int_to_str(low_depth_region[1]) + ' bp</td></tr>\n'
    else:
        no_low_depth_regions_help = 'No ' + window_str + ' windows in this reference fall ' + \
                                    'below the low depth threshold.'
        table += '  <tr title="' + no_low_depth_regions_help + '">' + \
                 '<th>No low depth regions</th></tr>'
    table += '</table>\n'

    table += '<br>\n'
    table += '<table width="100%">\n'
    if ref.high_depth_regions:
        high_depth_regions_help = 'These regions of the reference exceed the high depth ' + \
                                  'threshold.'
        table += '  <tr title="' + high_depth_regions_help + '">' + \
                 '<th>High depth regions</th></tr>'
        for i, high_depth_region in enumerate(ref.high_depth_regions):
            table += '      <tr title="' + high_depth_regions_help + '">' + \
                     '<td>' + int_to_str(i+1) + ')  ' + \
                     int_to_str(high_depth_region[0]) + ' bp to ' + \
                     int_to_str(high_depth_region[1]) + ' bp</td></tr>\n'
    else:
        no_high_depth_regions_help = 'No ' + window_str + ' windows in this reference exceed' + \
                                     ' the high depth threshold.'
        table += '  <tr title="' + no_high_depth_regions_help + '">' + \
                 '<th>No high depth regions</th></tr>'
    table += '</table>\n'

    return table


class Alignment(object):
    '''
    This class describes an alignment between a long read and a reference.
    '''
    def __init__(self, sam_line, read_dict, reference_dict, scoring_scheme):

        # Grab the important parts of the alignment from the SAM line.
        sam_parts = sam_line.split('\t')
        self.rev_comp = bool(int(sam_parts[1]) & 0x10)
        cigar_parts = re.findall(r'\d+\w', sam_parts[5])
        cigar_types = [x[-1] for x in cigar_parts]
        cigar_counts = [int(x[:-1]) for x in cigar_parts]

        read_name = sam_parts[0]
        if read_name not in read_dict:
            print()
            quit_with_error('the read ' + read_name + ' is in the SAM file but not in the '
                            'provided reads')

        self.read = read_dict[read_name]
        read_len = self.read.get_length()
        self.read_start_pos = self.get_start_soft_clips(cigar_parts)
        self.read_end_pos = self.read.get_length() - self.get_end_soft_clips(cigar_parts)
        self.read_end_gap = self.get_end_soft_clips(cigar_parts)

        ref_name = get_nice_header(sam_parts[2])
        if ref_name not in reference_dict:
            print()
            quit_with_error('the reference ' + ref_name + ' is in the SAM file but not in the '
                            'provided references')

        self.ref = reference_dict[get_nice_header(sam_parts[2])]
        ref_len = self.ref.get_length()
        self.ref_start_pos = int(sam_parts[3]) - 1
        self.ref_end_pos = self.ref_start_pos
        for i in range(len(cigar_types)):
            self.ref_end_pos += get_ref_shift_from_cigar_part(cigar_types[i], cigar_counts[i])
        if self.ref_end_pos > ref_len:
            self.ref_end_pos = ref_len
        self.ref_end_gap = ref_len - self.ref_end_pos

        self.ref_mismatch_positions = []
        self.ref_deletion_positions = []
        self.ref_insertion_positions = []

        # Remove the soft clipping parts of the CIGAR for tallying.
        if cigar_types[0] == 'S':
            cigar_types.pop(0)
            cigar_counts.pop(0)
        if cigar_types and cigar_types[-1] == 'S':
            cigar_types.pop()
            cigar_counts.pop()
        if not cigar_types:
            return

        if self.rev_comp:
            read_seq = reverse_complement(self.read.sequence)
        else:
            read_seq = self.read.sequence

        read_i = self.read_start_pos
        ref_i = self.ref_start_pos

        for i in range(len(cigar_types)):
            cigar_count = cigar_counts[i]
            cigar_type = cigar_types[i]

            # Insertions are only counted as a single error, regardless of size.
            if cigar_type == 'I':
                self.ref_insertion_positions += [ref_i] 
                read_i += cigar_count
            elif cigar_type == 'D':
                for i in range(cigar_count):
                    self.ref_deletion_positions.append(ref_i + i)
                ref_i += cigar_count
            else: # match/mismatch
                for _ in range(cigar_count):
                    # If all is good with the CIGAR, then we should never end up with a sequence
                    # index out of the sequence range. But a CIGAR error (which has occurred in
                    # GraphMap) can cause this, so check here.
                    if read_i >= read_len or ref_i >= ref_len:
                        break
                    if read_seq[read_i] != self.ref.sequence[ref_i]:
                        self.ref_mismatch_positions.append(ref_i)
                    read_i += 1
                    ref_i += 1

    def __repr__(self):
        read_start, read_end = self.read_start_end_positive_strand()
        return_str = self.read.name + ' (' + str(read_start) + '-' + str(read_end) + ', '
        if self.rev_comp:
            return_str += 'strand: -), '
        else:
            return_str += 'strand: +), '
        return_str += self.ref.name + ' (' + str(self.ref_start_pos) + '-' + \
                      str(self.ref_end_pos) + ')'
        error_count = len(self.ref_mismatch_positions) + len(self.ref_deletion_positions) + \
                      len(self.ref_insertion_positions)
        return_str += ', errors = ' + int_to_str(error_count)
        return return_str

    def get_start_soft_clips(self, cigar_parts):
        '''
        Returns the number of soft-clipped bases at the start of the alignment.
        '''
        if cigar_parts[0][-1] == 'S':
            return int(cigar_parts[0][:-1])
        else:
            return 0

    def get_end_soft_clips(self, cigar_parts):
        '''
        Returns the number of soft-clipped bases at the start of the alignment.
        '''
        if cigar_parts[-1][-1] == 'S':
            return int(cigar_parts[-1][:-1])
        else:
            return 0

    def read_start_end_positive_strand(self):
        '''
        This function returns the read start/end coordinates for the positive strand of the read.
        For alignments on the positive strand, this is just the normal start/end. But for
        alignments on the negative strand, the coordinates are flipped to the other side.
        '''
        if not self.rev_comp:
            return self.read_start_pos, self.read_end_pos
        else:
            start = self.read.get_length() - self.read_end_pos
            end = self.read.get_length() - self.read_start_pos
            return start, end


def get_ref_shift_from_cigar_part(cigar_type, cigar_count):
    '''
    This function returns how much a given cigar moves on a reference.
    Examples:
      * '5M' returns 5
      * '5S' returns 0
      * '5D' returns 5
      * '5I' returns 0
    '''
    if cigar_type == 'M' or cigar_type == 'D':
        return cigar_count
    else:
        return 0

def get_depth_min_and_max_distributions(read_lengths, reference_length, iterations, threads):
    distribution_str = simulate_depths(read_lengths, reference_length, iterations, threads)
    min_distribution_str, max_distribution_str = distribution_str.split(';')

    min_distribution_pieces = min_distribution_str.split(',')
    min_distribution = []
    for piece in min_distribution_pieces:
        piece_parts = piece.split(':')
        min_distribution.append((int(piece_parts[0]), float(piece_parts[1])))

    max_distribution_pieces = max_distribution_str.split(',')
    max_distribution = []
    for piece in max_distribution_pieces:
        piece_parts = piece.split(':')
        max_distribution.append((int(piece_parts[0]), float(piece_parts[1])))

    return min_distribution, max_distribution
