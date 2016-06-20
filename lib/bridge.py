'''
This module describes bridges - links between two single-copy segments in an assembly graph.
Bridges can come from multiple sources, so there are a few different classes which all share
the important members/methods (for duck-typing purposes).
'''

from multiprocessing.dummy import Pool as ThreadPool
import sys

from misc import float_to_str, reverse_complement, print_progress_line
from cpp_function_wrappers import multiple_sequence_alignment

class SpadesContigBridge(object):
    '''
    This class describes a bridge created from the contigs.paths file made by SPAdes.

    Quality is affected by:
      * How well the start and end segments' depths agree.
      * The depth consistency within the path (only applies to bridges where the path segments
        exclusively lead to the start/end segments).
    '''
    def __init__(self, graph, spades_contig_path):

        # The numbers of the two single-copy segments which are being bridged.
        self.start_segment = None
        self.end_segment = None

        # The path through the unbridged graph.
        self.graph_path = []

        # The bridge sequence, gotten from the graph path.
        self.bridge_sequence = ''

        # The bridge depth, a weighted mean of the start and end depths.
        self.depth = 0.0

        # A score used to determine the order of bridge application.
        self.quality = 20.0

        # The first and last values in spades_contig_path are the start and end segments. The
        # values in between are the path.
        self.graph_path = spades_contig_path
        self.start_segment = self.graph_path.pop(0)
        self.end_segment = self.graph_path.pop()
        self.bridge_sequence = graph.get_path_sequence(self.graph_path)
        start_seg = graph.segments[abs(self.start_segment)]
        end_seg = graph.segments[abs(self.end_segment)]

        # The start segment and end segment should agree in depth. If they don't, that's very bad,
        # so depth_disagreement is applied to quality twice (squared effect).
        depth_agreement = get_num_agreement(start_seg.depth, end_seg.depth)
        self.quality *= (depth_agreement * depth_agreement) # has squared effect on quality
        self.depth = get_mean_depth(start_seg, end_seg, graph)

        # If the segments in the path exclusively lead to the start and end segments (i.e they
        # cannot lead to any another segment), then we can also scale the quality based on the
        # depth consistency of the path. E.g. if a bridge path contains a segment 3 times and that
        # segment's depth also suggests about 3 times, that's good. If they don't agree, that's
        # bad.
        if path_is_self_contained(self.graph_path, self.start_segment, self.end_segment, graph):
            graph_path_pos_nums = list(set([abs(x) for x in self.graph_path]))
            for path_segment in graph_path_pos_nums:
                actual_depth = graph.segments[path_segment].depth
                expected_depth = graph_path_pos_nums.count(path_segment) * self.depth
                agreement = get_num_agreement(actual_depth, expected_depth)
                self.quality *= agreement

    def __repr__(self):
        return 'SPAdes contig bridge: ' + str(self.start_segment) + ' -> ' + \
               ', '.join([str(x) for x in self.graph_path]) + ' -> ' + str(self.end_segment) + \
               ' (quality = ' + float_to_str(self.quality, 2) + ')'


class LongReadBridge(object):
    '''
    This class describes a bridge created from long read alignments.
    '''
    def __init__(self, graph, start, end):

        # The numbers of the two single-copy segments which are being bridged.
        self.start_segment = start
        self.end_segment = end

        # The individual reads contributing to the bridge. The sequences/qualities are not for the
        # entire read, just the part in the bridge. The lengths are stored separately because
        # negative bridge lengths are possible (when the start and end segment alignments overlap).
        # In these cases we don't have any read sequences but will still need to know the lengths
        # (which will be negative).
        self.full_span_reads = []

        # The bridge can also contain incomplete read sequences which don't bridge the entire span
        # between the start and end segments. These are still useful as they can contribute to the
        # consensus sequence.
        self.start_only_reads = []
        self.end_only_reads = []

        # The consensus of all read sequences. If there is only one read, then this is the same as
        # that read's sequence. If there are multiple reads, this is hopefully a more accurate
        # sequence. If this is a case where the start and end segments overlap, then there will not
        # be a consensus sequence and consensus_length will be the mean of read_lengths (a negative
        # value).
        self.consensus_sequence = ''
        self.consensus_length = 0.0

        # The path through the unbridged graph, if one was found.
        self.graph_path = []

        # The bridge sequence, gotten from the graph path if a good path was found. Otherwise it's
        # from the consensus sequence.
        self.bridge_sequence = ''

        # The bridge depth, a weighted mean of the start and end depths.
        self.depth = 0.0

        # A score used to determine the order of bridge application.
        self.quality = 1.0

        self.graph = graph

    def __repr__(self):
        return 'long read bridge: ' + str(self.start_segment) + ' -> ' + \
               ', '.join([str(x) for x in self.graph_path]) + ' -> ' + str(self.end_segment) + \
               ' (quality = ' + float_to_str(self.quality, 2) + ')'

    def finalise(self, scoring_scheme):
        '''
        Determines the consensus sequence for the bridge, attempts to find it in the graph and
        assigns a quality score to the bridge. This is the performance-intensive step of long read
        bridging.
        ''' 
        output = '\n' # TEMP
        output += 'FINALISING BRIDGE\n' # TEMP
        output += '-----------------\n' # TEMP
        output += 'start: ' + str(self.start_segment) + '\n' # TEMP
        output += 'end:   ' + str(self.end_segment) + '\n' # TEMP
        output += 'start overlaps:\n' # TEMP
        for start_only_read in self.start_only_reads:
            output += '  ' + str(start_only_read) + '\n' # TEMP
        output += 'end overlaps:\n' # TEMP
        for end_only_read in self.end_only_reads:
            output += '  ' + str(end_only_read) + '\n' # TEMP
        output += 'full spans:\n' # TEMP
        for full_span_read in self.full_span_reads:
            output += '  ' + str(full_span_read) + '\n' # TEMP

        # Parition the full span reads into two groups: those with negative numbers (implying that
        # the two segments overlap) and those with actual sequences.
        full_spans_without_seq = []
        full_spans_with_seq = []
        for full_span in self.full_span_reads:
            if isinstance(full_span[0], int):
                full_spans_without_seq.append(full_span)
            else:
                full_spans_with_seq.append(full_span)

        # There shouldn't usually be both full spans with sequence and without. If there are some
        # of each, we'll throw out the minority group.
        if full_spans_with_seq and full_spans_without_seq:
            if len(full_spans_without_seq) > len(full_spans_with_seq):
                full_spans_with_seq = []
            else:
                full_spans_without_seq = []

        # For full spans with sequence, we perform a MSA and get a consensus sequence.
        if full_spans_with_seq:
            full_span_seqs = [x[0] for x in full_spans_with_seq]
            full_span_quals = [x[1] for x in full_spans_with_seq]
            start_only_seqs = [x[0] for x in self.start_only_reads]
            start_only_quals = [x[1] for x in self.start_only_reads]
            end_only_seqs = [x[0] for x in self.end_only_reads]
            end_only_quals = [x[1] for x in self.end_only_reads]

            consensus, full_span_scores, start_only_scores, end_only_scores = \
                                    multiple_sequence_alignment(full_span_seqs, full_span_quals,
                                                                start_only_seqs, start_only_quals,
                                                                end_only_seqs, end_only_quals,
                                                                scoring_scheme)
            output += 'consensus: ' + str(consensus) + '\n' # TEMP
            output += 'full span consensus scores: ' + str(full_span_scores) + '\n' # TEMP
            if start_only_scores: # TEMP
                output += 'start-only consensus scores: ' + str(start_only_scores) + '\n' # TEMP
            if end_only_scores: # TEMP
                output += 'end-only consensus scores: ' + str(end_only_scores) + '\n' # TEMP
            target_graph_path_length = len(consensus) + (2 * self.graph.overlap)

        # For full spans without sequence, we simply need a mean distance.
        elif full_spans_without_seq:
            mean_overlap = int(round(sum(x[0] for x in full_spans_without_seq) / \
                                     len(full_spans_without_seq)))
            output += 'mean overlap: ' + str(mean_overlap) + '\n' # TEMP
            target_graph_path_length = mean_overlap + (2 * self.graph.overlap)

        output += 'target graph path length: ' + str(target_graph_path_length) + '\n' # TEMP





























        output += '\n' # TEMP



        return output

    def contains_full_span_sequence(self):
        '''
        Some LongReadBridge objects bridge two close segments, and therefore do not actually have
        any bridging read sequence (just a negative number implying overlap). This function returns
        True if any full span read sequences exist and False if they are just negative numbers.
        '''
        for full_span_read in self.full_span_reads:
            seq_or_num = full_span_read[0]
            if not isinstance(seq_or_num, int):
                return True
        return False



class LoopUnrollingBridge(object):
    '''
    This class describes a bridge created from unrolling an assembly graph loop.

    Quality is affected by:
      * How well the start and end segments' depths agree.
      * How close the determined loop count is to a whole number.
      * The final loop count (higher counts get lower quality).
    '''
    def __init__(self, graph, start, end, middle, repeat):
        '''
        This constructor assumes the the start, end, middle and repeat segments form a simple loop
        in the graph supported by either a SPAdes contig or a long read alignment. It will use
        segment depths to determine the loop count and score the bridge's quality.
        '''
        # The numbers of the two single-copy segments which are being bridged.
        self.start_segment = start
        self.end_segment = end

        # The path through the unbridged graph.
        self.graph_path = []

        # The bridge sequence, gotten from the graph path.
        self.bridge_sequence = ''

        # The bridge depth, a weighted mean of the start and end depths.
        self.depth = 0.0

        # A score used to determine the order of bridge application. This value starts at the
        # maximum for a loop unrolling bridge and can only decrease as the constructor continues.
        self.quality = 10.0

        # Get the actual segments from the numbers. Since we are assuming they do form a simple
        # loop, we don't care about directionality.
        start_seg = graph.segments[abs(start)]
        end_seg = graph.segments[abs(end)]
        middle_seg = graph.segments[abs(middle)]
        repeat_seg = graph.segments[abs(repeat)]

        # The start segment and end segment should agree in depth. If they don't, that's very bad,
        # so depth_disagreement is applied to quality twice (squared effect).
        depth_agreement = get_num_agreement(start_seg.depth, end_seg.depth)
        self.quality *= (depth_agreement * depth_agreement) # has squared effect on quality

        # We'll use a mean loop count that's weighted by the middle and repeat segment lengths.
        self.depth = get_mean_depth(start_seg, end_seg, graph)
        loop_count_by_middle = middle_seg.depth / self.depth
        loop_count_by_repeat = (repeat_seg.depth - self.depth) / self.depth
        mean_loop_count = weighted_average(loop_count_by_middle, loop_count_by_repeat,
                                           middle_seg.get_length_no_overlap(graph.overlap),
                                           repeat_seg.get_length_no_overlap(graph.overlap))

        # If the average loop count is near a whole number, that's better. If it's near 0.5, that's
        # very bad!
        if mean_loop_count < 1.0:
            loop_count = 1
            closeness_to_whole_num = mean_loop_count
        else:
            loop_count = int(round(mean_loop_count))
            fractional_part = mean_loop_count % 1
            distance_from_whole_num = min(fractional_part, 1.0 - fractional_part)
            closeness_to_whole_num = 1.0 - (2.0 * distance_from_whole_num)
        self.quality *= closeness_to_whole_num

        # Finally, we reduce the quality for higher loop counts, as those are harder to call.
        loop_count_penalty = (1 / loop_count) ** 0.5
        self.quality *= loop_count_penalty

        self.graph_path = [repeat]
        for _ in range(loop_count):
            self.graph_path += [middle, repeat]
        self.bridge_sequence = graph.get_path_sequence(self.graph_path)

    def __repr__(self):
        return 'loop unrolling bridge: ' + str(self.start_segment) + ' -> ' + \
               ', '.join([str(x) for x in self.graph_path]) + ' -> ' + str(self.end_segment) + \
               ' (quality = ' + float_to_str(self.quality, 2) + ')'













def create_spades_contig_bridges(graph, single_copy_segments, verbosity):
    '''
    Builds graph bridges using the SPAdes contig paths.
    '''
    if verbosity > 0:
        print()
        print('Bridging graph with SPAdes contig paths')
        print('---------------------------------------', flush=True)

    bridge_path_set = set()
    single_copy_numbers = [x.number for x in single_copy_segments]
    for segment in single_copy_segments:
        for path in graph.paths.values():
            flipped_path = [-x for x in reversed(path)]
            contig_bridges = find_contig_bridges(segment.number, path, single_copy_numbers)
            contig_bridges += find_contig_bridges(segment.number, flipped_path, single_copy_numbers)
            for contig_bridge in contig_bridges:
                flipped_contig_bridge = [-x for x in reversed(contig_bridge)]
                contig_bridge_str = ','.join([str(x) for x in contig_bridge])
                flipped_contig_bridge_str = ','.join([str(x) for x in flipped_contig_bridge])
                if contig_bridge_str not in bridge_path_set and \
                   flipped_contig_bridge_str not in bridge_path_set:
                    if contig_bridge[0] < 0 and contig_bridge[-1] < 0:
                        bridge_path_set.add(flipped_contig_bridge_str)
                    else:
                        bridge_path_set.add(contig_bridge_str)

    bridge_path_list = sorted(list([[int(y) for y in x.split(',')] for x in bridge_path_set]))

    # If multiple bridge paths start with or end with the same segment, that implies a conflict
    # between SPADes' paths and our single-copy determination. Throw these bridges out.
    bridge_paths_by_start = {}
    bridge_paths_by_end = {}
    for path in bridge_path_list:
        start = path[0]
        end = path[-1]
        if start not in bridge_paths_by_start:
            bridge_paths_by_start[start] = []
        if end not in bridge_paths_by_end:
            bridge_paths_by_end[end] = []
        if -end not in bridge_paths_by_start:
            bridge_paths_by_start[-end] = []
        if -start not in bridge_paths_by_end:
            bridge_paths_by_end[-start] = []
        bridge_paths_by_start[start].append(path)
        bridge_paths_by_end[end].append(path)
        bridge_paths_by_start[-end].append(path)
        bridge_paths_by_end[-start].append(path)
    conflicting_paths = []
    for grouped_paths in bridge_paths_by_start.values():
        if len(grouped_paths) > 1:
            conflicting_paths += grouped_paths
    for grouped_paths in bridge_paths_by_end.values():
        if len(grouped_paths) > 1:
            conflicting_paths += grouped_paths
    conflicting_paths_no_dups = []
    for path in conflicting_paths:
        if path not in conflicting_paths_no_dups:
            conflicting_paths_no_dups.append(path)
    conflicting_paths = conflicting_paths_no_dups
    if verbosity > 1:
        print('Bridge paths in conflict with single-copy segments: ', end='')
        if conflicting_paths:
            print(', '.join([str(x) for x in conflicting_paths]))
        else:
            print('none')
        print()

    final_bridge_paths = [x for x in bridge_path_list if x not in conflicting_paths]
    if verbosity > 1:
        print('Final SPAdes contig bridge paths: ', end='')
        if final_bridge_paths:
            print(', '.join([str(x) for x in final_bridge_paths]))
        else:
            print('none')
        print()

    return [SpadesContigBridge(spades_contig_path=x, graph=graph) for x in final_bridge_paths]

def find_contig_bridges(segment_num, path, single_copy_numbers):
    '''
    This function returns a list of lists: every part of the path which starts on the segment_num
    and ends on any of the single_copy_numbers.
    '''
    bridge_paths = []
    indices = [i for i, x in enumerate(path) if abs(x) == segment_num]
    for index in indices:
        bridge_path = [path[index]]
        for i in range(index+1, len(path)):
            bridge_path.append(path[i])
            if path[i] in single_copy_numbers or -path[i] in single_copy_numbers:
                break
        else:
            bridge_path = []
        if bridge_path:
            bridge_paths.append(bridge_path)
    return bridge_paths

def create_loop_unrolling_bridges(graph, single_copy_segments, verbosity):
    '''
    This function creates loop unrolling bridges using the information in SPAdes paths.
    '''
    bridges = []
    simple_loops = graph.find_all_simple_loops()

    # A simple loop can either be caused by a repeat in one sequence (probably more typical) or by
    # a separate circular sequence which has some common sequence (less typical, but still very
    # possible: plasmids). We only want to unroll the former group, so we look for cases where the
    # loop's start or end is in a SPAdes contig path along with the middle. That implies that they
    # are on the same piece of DNA and can be unrolled.
    for start, end, middle, repeat in simple_loops:
        for path in graph.paths.values():
            joined = False
            flipped_path = [-x for x in reversed(path)]
            if (start in path and middle in path) or \
               (end in path and middle in path) or \
               (start in flipped_path and middle in flipped_path) or \
               (end in flipped_path and middle in flipped_path):
                joined = True
                break
        if not joined:
            continue

        # If the code got here, then things look good and we'll make a loop unrolling bridge!
        bridges.append(LoopUnrollingBridge(graph, start, end, middle, repeat))

    return bridges

def create_long_read_bridges(graph, read_dict, read_names, single_copy_segments, verbosity,
                             existing_bridges, min_scaled_score, threads, scoring_scheme):
    '''
    Makes bridges between single-copy segments using the alignments in the long reads.
    '''

    single_copy_seg_num_set = set()
    for seg in single_copy_segments:
        single_copy_seg_num_set.add(seg.number)

    # This dictionary will collect the read sequences which span between two single-copy segments.
    # These are the most useful sequences and will be used to either create a new bridge or enhance
    # an existing bridge.
    # Key = tuple of signed segment numbers (the segments being bridged)
    # Value = list of tuples containing the bridging sequence and the single-copy segment
    #         alignments.
    spanning_read_seqs = {}

    # This dictionary will collect all of the read sequences which don't manage to span between
    # two single-copy segments, but they do overlap one single-copy segment and can therefore be
    # useful for generating consensus sequences in a bridge.
    # Key = signed segment number, Value = list of tuples containing the sequence and alignment
    overlapping_read_seqs = {}

    print('\n\n\n\n\n\n') # TEMP
    allowed_overlap = round(int(1.1 * graph.overlap))
    for read_name in read_names:
        read = read_dict[read_name]
        alignments = get_single_copy_alignments(read, single_copy_seg_num_set, allowed_overlap,
                                                min_scaled_score)
        if not alignments:
            continue

        print('\n') # TEMP
        print('READ:', read) # TEMP
        print('  SEQUENCE:', read.sequence)
        print('  SINGLE-COPY SEGMENT ALIGNMENTS:') # TEMP 
        for alignment in alignments: # TEMP
            print('    ', alignment) # TEMP
        if len(alignments) > 1: # TEMP
            print('  BRIDGING SEQUENCES:') # TEMP

        # If the code got here, then we have some alignments to single-copy segments. We grab
        # neighbouring pairs of alignments, starting with the highest scoring ones and work our
        # way down. This means that we should have a pair for each neighbouring alignment, but
        # potentially also more distant pairs if the alignments are strong.
        already_added = set()
        sorted_alignments = sorted(alignments, key=lambda x: x.raw_score, reverse=True)
        available_alignments = []
        for alignment in sorted_alignments:
            available_alignments.append(alignment)
            available_alignments = sorted(available_alignments,
                                          key=lambda x: x.read_start_positive_strand())
            for i in range(len(available_alignments) - 1):
                alignment_1 = available_alignments[i]
                alignment_2 = available_alignments[i+1]

                # Standardise the order so we don't end up with both directions (e.g. 5 to -6 and
                # 6 to -5) in spanning_read_seqs.
                seg_nums, flipped = flip_segment_order(alignment_1.get_signed_ref_num(),
                                                       alignment_2.get_signed_ref_num())
                if seg_nums not in already_added:
                    bridge_start = alignment_1.read_end_positive_strand()
                    bridge_end = alignment_2.read_start_positive_strand()

                    if bridge_end > bridge_start:
                        bridge_seq = read.sequence[bridge_start:bridge_end]
                        bridge_qual = read.qualities[bridge_start:bridge_end]
                        if flipped:
                            bridge_seq = reverse_complement(bridge_seq)
                            bridge_qual = bridge_qual[::-1]
                    else:
                        bridge_seq = bridge_end - bridge_start # 0 or a negative number
                        bridge_qual = ''

                    if seg_nums not in spanning_read_seqs:
                        spanning_read_seqs[seg_nums] = []

                    spanning_read_seqs[seg_nums].append((bridge_seq, bridge_qual, alignment_1, alignment_2))
                    already_added.add(seg_nums)

                    print('    ', seg_nums[0], seg_nums[1], bridge_seq) # TEMP
                    print('    ', seg_nums[0], seg_nums[1], bridge_qual) # TEMP

        # At this point all of the alignments have been added and we are interested in the first
        # and last alignments (which may be the same if there's only one). If the read extends
        # past these alignments, then the overlapping part might be useful for consensus sequences.
        first_alignment = available_alignments[0]
        start_overlap, start_qual = first_alignment.get_start_overlapping_read_seq()
        if start_overlap:
            seg_num = -alignment.get_signed_ref_num()
            seq = reverse_complement(start_overlap)
            qual = start_qual[::-1]
            if seg_num not in overlapping_read_seqs:
                overlapping_read_seqs[seg_num] = []
            overlapping_read_seqs[seg_num].append((seq, qual, first_alignment))
            print('  START OVERLAPPING SEQUENCE:') # TEMP
            print('    ', seg_num, seq) # TEMP
            print('    ', seg_num, qual) # TEMP
        last_alignment = available_alignments[-1]
        end_overlap, end_qual = last_alignment.get_end_overlapping_read_seq()
        if end_overlap:
            seg_num = alignment.get_signed_ref_num()
            if seg_num not in overlapping_read_seqs:
                overlapping_read_seqs[seg_num] = []
            overlapping_read_seqs[seg_num].append((end_overlap, end_qual, last_alignment))
            print('  END OVERLAPPING SEQUENCE:') # TEMP
            print('    ', seg_num, end_overlap) # TEMP
            print('    ', seg_num, end_qual) # TEMP
        print('\n') # TEMP

    # If an bridge already exists for a spanning sequence, we add the sequence to the bridge. If
    # not, we create a new bridge and add it.
    new_bridges = []
    for seg_nums, span in spanning_read_seqs.items():
        start, end = seg_nums
        for existing_bridge in existing_bridges:
            if isinstance(existing_bridge, LongReadBridge) and \
               existing_bridge.start_segment == start and existing_bridge.end_segment == end:
                matching_bridge = existing_bridge
                break
        else:
            new_bridge = LongReadBridge(graph, start, end)
            new_bridges.append(new_bridge)
            matching_bridge = new_bridge
        matching_bridge.full_span_reads += span
    all_bridges = existing_bridges + new_bridges

    # Add overlapping sequences to appropriate bridges, but only if they have some full span
    # sequence (if their full span reads only have overlap-indicating negative numbers, then we
    # won't be doing a consensus sequence and don't need overlapping sequences).
    for seg_num, overlaps in overlapping_read_seqs.items():
        for bridge in all_bridges:
            if not isinstance(bridge, LongReadBridge) or not bridge.contains_full_span_sequence():
                continue
            start_overlap = (bridge.start_segment == seg_num)
            end_overlap = (bridge.end_segment == -seg_num)
            if start_overlap or end_overlap:
                for overlap in overlaps:
                    if start_overlap:
                        bridge.start_only_reads.append(overlap)
                    elif end_overlap:
                        overlap_seq, overlap_qual, alignment = overlap
                        bridge.start_only_reads.append((reverse_complement(overlap_seq),
                                                        overlap_qual[::-1], alignment))

    # Now we need to finalise the reads. This is the intensive step, as it involves creating a
    # consensus sequence, finding graph paths and doing alignments between the consensus and the
    # graph paths. We therefore use available threads to make this faster.
    long_read_bridges = [x for x in all_bridges if isinstance(x, LongReadBridge)]
    num_long_read_bridges = len(long_read_bridges)
    completed_count = 0
    if verbosity == 1:
        print_progress_line(0, num_long_read_bridges, prefix='Bridge: ')
    completed_count = 0
    if threads == 1:
        for bridge in long_read_bridges:
            output = bridge.finalise(scoring_scheme)
            completed_count += 1
            if verbosity == 1:
                print_progress_line(completed_count, num_long_read_bridges, prefix='Bridge: ')
            if verbosity > 1:
                print(output, end='')
    else:
        pool = ThreadPool(threads)
        arg_list = []
        for bridge in long_read_bridges:
            arg_list.append((bridge, scoring_scheme))
        for output in pool.imap(finalise_bridge, arg_list, 1):
            completed_count += 1
            if verbosity == 1:
                print_progress_line(completed_count, num_long_read_bridges, prefix='Bridge: ')
            if verbosity > 1:
                print(output, end='')

    if verbosity == 1:
        print('\n')

    return all_bridges

def get_num_agreement(num_1, num_2):
    '''
    Returns a value between 0.0 and 1.0 describing how well the numbers agree.
    1.0 is perfect agreement and 0.0 is the worst.
    '''
    if num_1 == 0.0 and num_2 == 0.0:
        return 1.0
    if num_1 < 0.0 and num_2 < 0.0:
        num_1 = -num_1
        num_2 = -num_2
    if num_1 * num_2 < 0.0:
        return 0.0
    return min(num_1, num_2) / max(num_1, num_2)

def get_mean_depth(seg_1, seg_2, graph):
    '''
    Returns the mean depth of the two segments, weighted by their length.
    '''
    return weighted_average(seg_1.depth, seg_2.depth,
                            seg_1.get_length_no_overlap(graph.overlap),
                            seg_2.get_length_no_overlap(graph.overlap))

def weighted_average(num_1, num_2, weight_1, weight_2):
    '''
    A simple weighted mean of two numbers.
    '''
    weight_sum = weight_1 + weight_2
    return num_1 * (weight_1 / weight_sum) + num_2 * (weight_2 / weight_sum)


def path_is_self_contained(path, start, end, graph):
    '''
    Returns True if the path segments are only connected to each other and the start/end segments.
    If they are connected to anything else, it returns False.
    '''
    all_numbers_in_path = set()
    all_numbers_in_path.add(abs(start))
    all_numbers_in_path.add(abs(end))
    for segment in path:
        all_numbers_in_path.add(abs(segment))
    for segment in path:
        connected_segments = graph.get_connected_segments(segment)
        for connected_segment in connected_segments:
            if connected_segment not in all_numbers_in_path:
                return False
    return True

def flip_segment_order(seg_num_1, seg_num_2):
    '''
    Given two segment numbers, this function possibly flips them around. It returns the new numbers
    (either unchanged or flipped) and whether or not a flip took place. The decision is somewhat
    arbitrary, but it needs to be consistent so when we collect bridging read sequences they are
    always in the same direction.
    '''
    if seg_num_1 > 0 and seg_num_2 > 0:
        flip = False
    elif seg_num_1 < 0 and seg_num_2 < 0:
        flip = True
    elif seg_num_1 < 0: # only seg_num_1 is negative
        flip = abs(seg_num_1) > abs(seg_num_2)
    else: # only seg_num_2 is negative
        flip = abs(seg_num_2) > abs(seg_num_1)
    if flip:
        return (-seg_num_2, -seg_num_1), True
    else:
        return (seg_num_1, seg_num_2), False

def get_single_copy_alignments(read, single_copy_num_set, allowed_overlap, min_scaled_score):
    '''
    Returns a list of single-copy segment alignments for the read.
    '''
    sc_alignments = {}
    for alignment in read.alignments:
        ref_num = alignment.ref.number
        if ref_num not in single_copy_num_set:
            continue
        if alignment.scaled_score < min_scaled_score:
            continue
        if ref_num not in sc_alignments:
            sc_alignments[ref_num] = []
        sc_alignments[ref_num].append(alignment)

    # If any two alignments are to the same segment, that implies that one is bogus and so we
    # should only include the higher scoring one. Unless, however, the two alignments to the same
    # segment overlap opposite ends of the read, in which case they may both be valid.
    final_alignments = []
    for alignments in sc_alignments.values():

        # In most cases, there will be only one alignment per reference number, so this part is
        # simple.
        alignments = sorted(alignments, key=lambda x: x.scaled_score, reverse=True)
        best_alignment = alignments[0]
        final_alignments.append(best_alignment)

        # However, multiple alignments are possible. In some cases, this means alignments other
        # than the best should be thrown out (because the segment is supposed to be single-copy).
        # But there are cases where two alignments to the same single-copy reference can be valid.
        # This is when the start and end of the single-copy segment are close to each other in a
        # circular piece of DNA and the read overlaps with both ends.
        if len(alignments) > 1:
            second_best_alignment = alignments[1]
            combined_len = best_alignment.get_aligned_ref_length() + \
                           second_best_alignment.get_aligned_ref_length()
            if combined_len <= best_alignment.ref.get_length() + allowed_overlap:
                if (best_alignment.ref_start_pos > 0 and second_best_alignment.ref_end_gap > 0) or \
                   (best_alignment.ref_end_gap > 0 and second_best_alignment.ref_start_pos > 0):
                    final_alignments.append(second_best_alignment)
    return final_alignments

def finalise_bridge(both_args):
    bridge, scoring_scheme = both_args
    return bridge.finalise(scoring_scheme)