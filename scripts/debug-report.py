#!/usr/bin/env python2.7

from __future__ import print_function
import sys, os
from collections import namedtuple, defaultdict
from cStringIO import StringIO
from itertools import combinations

import networkx as nx
import pylab
from Bio import Phylo

from utils.nucmer_parser import *

Edge = namedtuple("Edge", ["start", "end"])
Adjacency = namedtuple("Adjacency", ["left", "right", "infinite"])

def verify_alignment(alignment, contigs):
    problematic_contigs = []
    by_name = defaultdict(list)
    for entry in alignment:
        by_name[entry.contig_id].append(entry)
    for name in contigs:
        if len(by_name[name]) > 1:
            hits = list(map(lambda e: (e.s_ref, e.len_qry), by_name[name]))
            print("WARNING: Duplicated contig", name, hits, file=sys.stderr)
            problematic_contigs.append(name)
        if not by_name[name]:
            print("WARNING: Contig", name, "is not aligned", file=sys.stderr)
            problematic_contigs.append(name)
    return problematic_contigs


def get_true_adjacencies(alignment, contig_permutations,
                         break_contigs, circular):
    by_chr = group_by_chr(alignment)
    adjacencies = []

    for chr_name, entries in by_chr.items():
        prev_block = None
        prev_contig = None

        entries.append(entries[0])
        for hit in entries:
            if prev_contig in break_contigs or hit.contig_id in break_contigs:
                continue

            sign = 1 if hit.e_qry > hit.s_qry else -1
            blocks = contig_permutations[hit.contig_id]

            if sign < 0:
                blocks = list(map(lambda x: -x, blocks))[::-1]
            if prev_block:
                adjacencies.append(Adjacency(-prev_block, blocks[0], False))
            prev_block = blocks[-1]
            prev_contig = hit.contig_id

        if entries and not circular:
            adjacencies[-1] = Adjacency(adjacencies[-1].left,
                                        adjacencies[-1].right, True)

    return adjacencies


def get_contig_permutations(filename):
    contigs = {}
    for line in open(filename, "r"):
        line = line.strip()
        if not line:
            continue

        if line.startswith(">"):
            name = line[1:]
        else:
            blocks = line.split(" ")[:-1]
            contigs[name] = list(map(int, blocks))
    return contigs


def output_edges(edges, out_file):
    fout = open(out_file, "w")
    fout.write("graph {\n")
    for (v1, v2, inf) in edges:
        label = "oo" if inf else ""
        fout.write("{0} -- {1} [label=\"{2}\"];\n".format(v1, v2, label))
    fout.write("}")


def g2c(genome_id):
    if genome_id not in g2c.table:
        g2c.table[genome_id] = g2c.colors[0]
        g2c.colors = g2c.colors[1:] + g2c.colors[:1] #rotate list
    return g2c.table[genome_id]
g2c.colors = ["green", "blue", "yellow", "cyan", "magnetta"]
g2c.table = {}


def compose_breakpoint_graph(base_dot, predicted_dot, true_edges):
    base_graph = nx.read_dot(base_dot)
    predicted_edges = nx.read_dot(predicted_dot)
    out_graph = nx.MultiGraph()

    for v1, v2, data in base_graph.edges_iter(data=True):
        color = g2c(data["genome_id"])
        out_graph.add_edge(v1, v2, color=color)
    for v1, v2 in predicted_edges.edges_iter():
        out_graph.add_edge(v1, v2, color="red", style="dashed")
    for (v1, v2, infinite) in true_edges:
        label = "oo" if infinite else ""
        out_graph.add_edge(str(v1), str(v2), color="red",
                           style="bold", label=label)

    return out_graph


def output_graph(graph, output_dir, only_predicted):
    subgraphs = nx.connected_component_subgraphs(graph)
    for comp_id, subgr in enumerate(subgraphs):
        if len(subgr) == 2:
            continue

        if only_predicted:
            to_show = False
            for v1, v2, data in subgr.edges_iter(data=True):
                if data.get("style") == "dashed":
                    to_show = True
                    break
            if not to_show:
                continue

        comp_file = os.path.join(output_dir, "comp{0}-bg.png".format(comp_id))
        agraph = nx.to_agraph(subgr)
        agraph.layout(prog="dot")
        agraph.draw(comp_file)


def read_scaffold_file(file):
    scaffold = set()
    with open(file, "r") as input:
        for line in input:
            temp = line.strip('\n ')
            if temp[0] != '>':
                scaffold.add(temp)
    return scaffold

def my_has_path(graph, ordered_contigs, src, dst):
    visited = set()

    def dfs(vertex):
        visited.add(vertex)

        for _, u in graph.edges(vertex):
            if u == dst:
                return True
            elif u not in visited and str(u)[1:] not in ordered_contigs:
                if dfs(u):
                    return True
        return False

    return dfs(src)


def add_overlap_edges(graph, overlap_dot, contigs_file):
    contigs = get_contig_permutations(contigs_file)
    contig_begins = {}
    contig_ends = {}
    for name, blocks in contigs.items():
        contig_begins[blocks[0]] = "+" + name
        contig_begins[-blocks[-1]] = "-" + name
        contig_ends[-blocks[-1]] = "+" + name
        contig_ends[blocks[0]] = "-" + name

    overlap_graph = nx.read_dot(overlap_dot)

    subgraphs = nx.connected_component_subgraphs(graph)
    for subgr in subgraphs:
        for v1, v2 in combinations(subgr.nodes(), 2):
            v1, v2 = int(v1), int(v2)

            if v1 in contig_ends and v2 in contig_begins:
                src = contig_ends[v1]
                dst = contig_begins[v2]
            elif v2 in contig_ends and v1 in contig_begins:
                src = contig_ends[v2]
                dst = contig_begins[v1]
            else:
                continue

            if not (overlap_graph.has_node(src) and
                    overlap_graph.has_node(dst)):
                continue

            if not nx.has_path(overlap_graph, src, dst):
                continue

            if my_has_path(overlap_graph, contigs, src, dst):
                graph.add_edge(str(v1), str(v2), weight=0.1)

            '''paths = list(nx.all_simple_paths(overlap_graph, src, dst, 10))
            for path in paths:
                is_good = True
                len_path = 0
                for p in path[1:-1]:
                    len_path += 1
                    if p[1:] in contigs:
                        is_good = False
                        break

                if is_good:
                    graph.add_edge(str(v1), str(v2), label=len_path,
                                       weight=0.1)
                    break
            '''

def draw_phylogeny(phylogeny_txt, out_file):
    tree_string, target_name = open(phylogeny_txt, "r").read().splitlines()
    g2c.table[target_name] = "red"

    tree = Phylo.read(StringIO(tree_string), "newick")
    tree.clade.branch_length = 0
    for clade in tree.find_clades():
        if clade.is_terminal():
            clade.color = g2c(clade.name)
    tree.ladderize()
    pylab.rcParams["lines.linewidth"] = 3.0
    Phylo.draw(tree, do_show=False)

    pylab.savefig(out_file)


def do_job(nucmer_coords, debug_dir, circular, only_predicted):
    used_contigs = os.path.join(debug_dir, "used_contigs.txt")
    true_adj_out = os.path.join(debug_dir, "true_edges.dot")
    base_dot = os.path.join(debug_dir, "breakpoint_graph.dot")
    overlap_dot = os.path.join(debug_dir, "../../contigs_overlap.dot")
    predicted_dot = os.path.join(debug_dir, "predicted_edges.dot")
    phylogeny_in = os.path.join(debug_dir, "phylogeny.txt")
    phylogeny_out = os.path.join(debug_dir, "phylogeny.png")

    draw_phylogeny(phylogeny_in, phylogeny_out)

    contigs = get_contig_permutations(used_contigs)
    if nucmer_coords != "-":
        alignment = parse_nucmer_coords(nucmer_coords)
        alignment = list(filter(lambda e: e.contig_id in contigs, alignment))
        #alignment = join_collinear(alignment)
        alignment = filter_by_coverage(alignment, 0.7)
        alignment = join_collinear(alignment)
        break_contigs = verify_alignment(alignment, contigs)
        true_adj = get_true_adjacencies(alignment, contigs,
                                        break_contigs, circular)
    else:
        true_adj = []

    output_edges(true_adj, true_adj_out)
    g = compose_breakpoint_graph(base_dot, predicted_dot, true_adj)
    if os.path.exists(overlap_dot):
        add_overlap_edges(g, overlap_dot, used_contigs)
    output_graph(g, debug_dir, only_predicted)


def main():
    if len(sys.argv) < 3:
        print("Usage: debug_report.py <nucmer_coords> <debug_dir> "
              "[--circular] [--predicted]")
        return

    nucmer_coords = sys.argv[1]
    debug_dir = sys.argv[2]
    circular = True if "--circular" in sys.argv else False
    only_predicted = True if "--predicted" in sys.argv else False
    do_job(nucmer_coords, debug_dir, circular, only_predicted)

if __name__ == "__main__":
    main()
