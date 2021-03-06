#!/usr/bin/env python
#
# This file is part of pybgpstream
#
# CAIDA, UC San Diego
# bgpstream-info@caida.org
#
# Copyright (C) 2015 The Regents of the University of California.
# Authors: Danilo Giordano, Alistair King
#
# This program is free software; you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation; either version 2 of the License, or (at your option) any later
# version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.  See the GNU General Public License for more
# details.
#
# You should have received a copy of the GNU General Public License along with
# this program.  If not, see <http://www.gnu.org/licenses/>.
#
#
# Submit this script to spark like so:
# spark-submit --master=local[4] spark-community.py --start-time=1451606400 --end-time=1451779200 -t updates -c route-views.sg
#

import argparse
import csv
from datetime import datetime
import json
import math
from itertools import groupby,chain
from pyspark import SparkConf, SparkContext
from _pybgpstream import BGPStream, BGPRecord
import sys
import urllib2
import numpy as np

# Output one data point per day
RESULT_GRANULARITY = 3600*24

# When processing RIBs, split days into 4hr chunks for RV, 8hrs for RIS
RV_RIB_PROCESSING_GRANULARITY = 3600*4
RIS_RIB_PROCESSING_GRANULARITY = 3600*8
# When processing updates, split days into 2hr chunks
UPD_PROCESSING_GRANULARITY = 3600*2

# The BGPStream broker service URL to query to get collector list from
COLLECTORS_URL = "http://bgpstream.caida.org/broker/meta/collectors"
# We only care about the two major projects
PROJECTS = ('routeviews', 'ris')


# Query the BGPStream broker and identify the collectors that are available
def get_collectors():
    response = urllib2.urlopen(COLLECTORS_URL)
    data = json.load(response)
    results = []
    for coll in data['data']['collectors']:
        if data['data']['collectors'][coll]['project'] in PROJECTS:
            results.append(coll)
    return results


# takes a record and an elem and builds a peer signature string that is globally
# unique.
def peer_signature(record, elem):
    return record.project, record.collector, elem.peer_asn, elem.peer_address

def run_bgpstream(args):
    (collector, start_time, end_time, data_type) = args

    # initialize and configure BGPStream
    stream = BGPStream()
    rec = BGPRecord()
    stream.add_filter('collector', collector)
    # NB: BGPStream uses inclusive/inclusive intervals, so subtract one off the
    # end time since we are using inclusive/exclusive intervals
    stream.add_interval_filter(start_time, end_time-1)
    stream.add_filter('record-type', data_type)
    stream.start()

    # per-peer data
    peers_data = {}

    # loop over all records in the stream
    while stream.get_next_record(rec):
        elem = rec.get_next_elem()
        # loop over all elems in the record
        while elem:
            # create a peer signature for this elem
            sig = peer_signature(rec, elem)
            peer_asn =  elem.peer_asn
            # if this is the first time we have ever seen this peer, create
            # an empty result: (Pfxs_v4_set                    ,  Pfxs_v6_set, 
            #                   Community_Pfxs_v4_set          ,  Community_Pfxs_v6_set,
            #                   Internal_Community_Pfxs_v4_set ,  Internal_Community_Pfxs_v6_set,
            #                   External_Community_Pfxs_v4_set ,  External_Community_Pfxs_v6_set,
            #                   Community_ASNs_v4_set          ,  Community_ASNs_v6_set,
            #                   Community_v4_set               ,  Community_v6_set)

            if sig not in peers_data:
                peers_data[sig] =[set(),set(),
                                   set(),set(),
                                   set(),set(),
                                   set(),set(),
                                   set(),set(),
                                   set(),set()]


            pfx=""
            if('prefix' in elem.fields):            
                pfx=elem.fields['prefix'];                                    
                if(":" in pfx):
                    peers_data[sig][1].add(pfx)

                else:
                    peers_data[sig][0].add(pfx)
            
            #check if community
            if('communities' in elem.fields and len(elem.fields['communities'])!=0):
                if(":" in pfx):
                    peers_data[sig][3].add(pfx)
                    for community in elem.fields['communities']: 
                        community_asn=community['asn']                   
                        value=community['value']                 
                        if(community_asn == peer_asn):#internal
                            peers_data[sig][5].add(pfx)
                        else:#external
                            peers_data[sig][7].add(pfx)
                          
                        peers_data[sig][9].add(community_asn)
                        peers_data[sig][11].add((community_asn,value))
                elif(pfx!=""):
                    peers_data[sig][2].add(pfx)
                    for community in elem.fields['communities']: 
                        community_asn=community['asn']                   
                        value=community['value']                 
                        if(community_asn == peer_asn):#internal
                            peers_data[sig][4].add(pfx)
                        else:#external
                            peers_data[sig][6].add(pfx)
                          
                        peers_data[sig][8].add(community_asn)
                        peers_data[sig][10].add((community_asn,value))


            elem = rec.get_next_elem()

    # the time in the output row is truncated down to a multiple of
    # RESULT_GRANULARITY so that slices can be merged correctly
    start_time = \
        int(math.floor(start_time/RESULT_GRANULARITY) * RESULT_GRANULARITY)

    # for each peer that we processed data for, create an output row
    return [((start_time, collector, p), (peers_data[p])) for p in peers_data]


# takes a start time, an end time, and a partition length and splits the time
# range up into slices, each of len seconds. the interval is assumed to be a
# multiple of the len
def partition_time(start_time, end_time, len):
    slices = []
    while start_time < end_time:
        slices.append((start_time, start_time+len))
        start_time += len
    return slices


# takes two result tuples, each of the format:
# (Pfxs_v4_set                   ,  Pfxs_v6_set, 
# Community_Pfxs_v4_set          ,  Community_Pfxs_v6_set,
# Internal_Community_Pfxs_v4_set ,  Internal_Community_Pfxs_v6_set,
# External_Community_Pfxs_v4_set ,  External_Community_Pfxs_v6_set,
# Community_ASNs_v4_set          ,  Community_ASNs_v6_set,
# Community_v4_set               ,  Community_v6_set)
# and returns a single result tuple which is the sum of the two inputs.
# len(result_x) is assumed to be the same length as len(result_y)
def merge_results(result_x, result_y):

    return [set(chain(result_x[i],result_y[i])) for i in range(0, len(result_x))]
       

# takes a result row:
# ((time, collector, peer), (Pfxs_v4_set                    ,  Pfxs_v6_set, 
#                            Community_Pfxs_v4_set          ,  Community_Pfxs_v6_set,
#                            Internal_Community_Pfxs_v4_set ,  Internal_Community_Pfxs_v6_set,
#                            External_Community_Pfxs_v4_set ,  External_Community_Pfxs_v6_set,
#                            Community_ASNs_v4_set          ,  Community_ASNs_v6_set,
#                            Community_v4_set               ,  Community_v6_set))
# and returns
# ((time, collector), (Pfxs_v4_set                    ,  Pfxs_v6_set, 
#                      Community_Pfxs_v4_set          ,  Community_Pfxs_v6_set,
#                      Internal_Community_Pfxs_v4_set ,  Internal_Community_Pfxs_v6_set,
#                      External_Community_Pfxs_v4_set ,  External_Community_Pfxs_v6_set,
#                      Community_ASNs_v4_set          ,  Community_ASNs_v6_set,
#                      Community_v4_set               ,  Community_v6_set))
def map_per_collector(row):
    return (row[0][0], row[0][1]), row[1]


# takes a result row:
# ((time, collector), (Pfxs_v4_set                    ,  Pfxs_v6_set, 
#                      Community_Pfxs_v4_set          ,  Community_Pfxs_v6_set,
#                      Internal_Community_Pfxs_v4_set ,  Internal_Community_Pfxs_v6_set,
#                      External_Community_Pfxs_v4_set ,  External_Community_Pfxs_v6_set,
#                      Community_ASNs_v4_set          ,  Community_ASNs_v6_set,
#                      Community_v4_set               ,  Community_v6_set))
# and returns
# ((time), (Pfxs_v4_set                    ,  Pfxs_v6_set, 
#           Community_Pfxs_v4_set          ,  Community_Pfxs_v6_set,
#           Internal_Community_Pfxs_v4_set ,  Internal_Community_Pfxs_v6_set,
#           External_Community_Pfxs_v4_set ,  External_Community_Pfxs_v6_set,
#           Community_ASNs_v4_set          ,  Community_ASNs_v6_set,
#           Community_v4_set               ,  Community_v6_set))
def map_per_time(row):
    return (row[0][0]), row[1]

def analyze(start_time, end_time, data_type, outdir,
            collector=None, num_cores=None, memory=None):

    # round start time down to nearest day
    start_time = \
        int(math.floor(start_time/RESULT_GRANULARITY) * RESULT_GRANULARITY)
    # round end time up to nearest day
    end_time = int(math.ceil(end_time/RESULT_GRANULARITY) * RESULT_GRANULARITY)
    # generate a list of time slices to process
    time_slices = partition_time(start_time, end_time, RESULT_GRANULARITY)

    start_str = datetime.utcfromtimestamp(start_time).strftime('%Y-%m-%d')
    end_str = datetime.utcfromtimestamp(end_time).strftime('%Y-%m-%d')

    # establish the spark context
    conf = SparkConf()\
        .setAppName("community.%s.%s-%s" % (data_type, start_str, end_str))\
        .set("spark.files.overwrite", "true")
    if memory:
        conf.set("spark.executor.memory", str(memory)+"g")
    sc = SparkContext(conf=conf)

    # either use the collector argument, or default to using all collectors
    # that the BGPStream broker knows about
    collectors = [collector]
    if not collector:
        collectors = get_collectors()

    # build our input for spark -- a set of BGPStream configurations to process
    # in parallel
    bs_configs = []
    for time_slice in time_slices:
        for collector in collectors:
            (start, end) = time_slice
            while start < end:
                duration = UPD_PROCESSING_GRANULARITY
                if type == 'ribs':
                    if 'rrc' in collector:
                        duration = RIS_RIB_PROCESSING_GRANULARITY
                    else:
                        duration = RV_RIB_PROCESSING_GRANULARITY
                slice_end = min(start+duration, end)
                bs_configs.append((collector, start, slice_end, data_type))
                start += duration

    # debugging
    sys.stderr.write(str(bs_configs) + "\n")

    # we need to instruct spark to slice up our input more aggressively than
    # it normally would since we know that each row will take some time to
    # process. to do this we either use 4x the number of cores available,
    # or we split once per row. Once per row will be most efficient, but we
    # have seem problems with the JVM exploding when numSlices is huge (it
    # tries to create thousands of threads...)
    slice_cnt = len(bs_configs)
    if num_cores:
        slice_cnt = num_cores*4

    # instruct spark to create an RDD from our BGPStream config list
    bs_rdd = sc.parallelize(bs_configs, numSlices=slice_cnt)

    # step 1: use BGPStream to process BGP data
    # output will be a list:
    #  ((time, collector,peer), (Pfxs_v4_set                    ,  Pfxs_v6_set, 
    #                            Community_Pfxs_v4_set          ,  Community_Pfxs_v6_set,
    #                            Internal_Community_Pfxs_v4_set ,  Internal_Community_Pfxs_v6_set,
    #                            External_Community_Pfxs_v4_set ,  External_Community_Pfxs_v6_set,
    #                            Community_ASNs_v4_set          ,  Community_ASNs_v6_set,
    #                            Community_v4_set               ,  Community_v6_set))    
    raw_results = bs_rdd.flatMap(run_bgpstream)

    # since we split the processing by time, there will be several rows for
    # each peer.
    reduced_time_collector_peer = raw_results.reduceByKey(merge_results)
    # we will use this result multiple times, so persist it
    reduced_time_collector_peer.persist()

    # collect the reduced time-collector-peer results back to the driver
    # we take results that are in the form:
    # ((time, collector,peer), (len(Pfxs_v4_set)                    ,  len(Pfxs_v6_set), 
    #                           len(Community_Pfxs_v4_set)          ,  len(Community_Pfxs_v6_set),
    #                           len(Internal_Community_Pfxs_v4_set) ,  len(Internal_Community_Pfxs_v6_set),
    #                           len(External_Community_Pfxs_v4_set) ,  len(External_Community_Pfxs_v6_set),
    #                           len(Community_ASNs_v4_set)          ,  len(Community_ASNs_v6_set),
    #                           len(Community_v4_set)               ,  len(Community_v6_set))        
    # and map them into:
    # (time, collector,peer) =>  (len(Pfxs_v4_set)                    ,  len(Pfxs_v6_set), 
    #                             len(Community_Pfxs_v4_set)          ,  len(Community_Pfxs_v6_set),
    #                             len(Internal_Community_Pfxs_v4_set) ,  len(Internal_Community_Pfxs_v6_set),
    #                             len(External_Community_Pfxs_v4_set) ,  len(External_Community_Pfxs_v6_set),
    #                             len(Community_ASNs_v4_set)          ,  len(Community_ASNs_v6_set),
    #                             len(Community_v4_set)               ,  len(Community_v6_set))      
    
    final_time_collector_peer = reduced_time_collector_peer\
        .mapValues(lambda x: [len(x[i]) for i in range(0,len(x))]).collectAsMap()

    # take the time-collector-peer result and map it into a new RDD which
    # is time-collector. after the 'map' stage there will be duplicate
    # time-collector keys, so perform a reduction as we did before
    reduced_time_collector = reduced_time_collector_peer\
        .map(map_per_collector).reduceByKey(merge_results)
    reduced_time_collector.persist()

    # collect the reduced time-collector results back to the driver
    # we take results that are in the form:
    # ((time, collector), (len(Pfxs_v4_set)                    ,  len(Pfxs_v6_set), 
    #                      len(Community_Pfxs_v4_set)          ,  len(Community_Pfxs_v6_set),
    #                      len(Internal_Community_Pfxs_v4_set) ,  len(Internal_Community_Pfxs_v6_set),
    #                      len(External_Community_Pfxs_v4_set) ,  len(External_Community_Pfxs_v6_set),
    #                      len(Community_ASNs_v4_set)          ,  len(Community_ASNs_v6_set),
    #                      len(Community_v4_set)               ,  len(Community_v6_set))     
    # and map them into:
    # (time, collector) =>  (len(Pfxs_v4_set)                    ,  len(Pfxs_v6_set), 
    #                        len(Community_Pfxs_v4_set)          ,  len(Community_Pfxs_v6_set),
    #                        len(Internal_Community_Pfxs_v4_set) ,  len(Internal_Community_Pfxs_v6_set),
    #                        len(External_Community_Pfxs_v4_set) ,  len(External_Community_Pfxs_v6_set),
    #                        len(Community_ASNs_v4_set)          ,  len(Community_ASNs_v6_set),
    #                        len(Community_v4_set)               ,  len(Community_v6_set))        

    final_time_collector = reduced_time_collector\
        .mapValues(lambda x: [len(x[i]) for i in range(0,len(x))]).collectAsMap()

    # take the time-collector result and map it into a new RDD which is keyed
    # by time only (i.e. a global view). again we need to reduce after the map
    # stage.
    reduced_time = reduced_time_collector.map(map_per_time)\
        .reduceByKey(merge_results)

    # collect the reduced time-only results back to the driver
    # we take results that are in the form:
    # ((time), (len(Pfxs_v4_set)                    ,  len(Pfxs_v6_set), 
    #           len(Community_Pfxs_v4_set)          ,  len(Community_Pfxs_v6_set),
    #           len(Internal_Community_Pfxs_v4_set) ,  len(Internal_Community_Pfxs_v6_set),
    #           len(External_Community_Pfxs_v4_set) ,  len(External_Community_Pfxs_v6_set),
    #           len(Community_ASNs_v4_set)          ,  len(Community_ASNs_v6_set),
    #           len(Community_v4_set)               ,  len(Community_v6_set))     
    # and map them into:
    # time =>  (len(Pfxs_v4_set)                    ,  len(Pfxs_v6_set), 
    #           len(Community_Pfxs_v4_set)          ,  len(Community_Pfxs_v6_set),
    #           len(Internal_Community_Pfxs_v4_set) ,  len(Internal_Community_Pfxs_v6_set),
    #           len(External_Community_Pfxs_v4_set) ,  len(External_Community_Pfxs_v6_set),
    #           len(Community_ASNs_v4_set)          ,  len(Community_ASNs_v6_set),
    #           len(Community_v4_set)               ,  len(Community_v6_set))       

    final_time = reduced_time.mapValues(lambda x: [len(x[i]) for i in range(0,len(x))]).collectAsMap()

    # build the output file name
    outfile = "%s/bgpstream-community.%s.%s-%s.csv" %\
              (outdir, data_type, start_str, end_str)

    with open(outfile, 'wb') as csvfile:
        w = csv.writer(csvfile)
        w.writerow(["Time", "Collector", "Peer", "Total_IPv4", "Total_IPv6", 
                    "#Pfx_v4_ANY", "#Pfx_v6_ANY", "#Pfx_v4_Int", "#Pfx_v6_Int",
                    "#Pfx_v4_Ext", "#Pfx_v6_Ext", "#ASN_v4", "#ASN_v6",
                    "#ASN-Value_v4", "#ASN-Value_v6"])        
        
        # write out the per-peer statistics
        for key in final_time_collector_peer:
            (ts, coll, peer) = key
            (tot_v4,tot_v6, pfxs_v4, pfxs_v6, min_v4, mean_v4, max_v4, std_v4
             , min_v6, mean_v6, max_v6, std_v6) = final_time_collector_peer[key]
            w.writerow([ts, coll, "AS"+str(peer[2])+"-"+peer[3],
                        tot_v4,tot_v6, pfxs_v4, pfxs_v6, min_v4, mean_v4, max_v4, std_v4
                        , min_v6, mean_v6, max_v6, std_v6])

        # write out the per-collector statistics
        for key in final_time_collector:
            (ts, coll) = key
            (tot_v4,tot_v6, pfxs_v4, pfxs_v6, min_v4, mean_v4, max_v4, std_v4
             , min_v6, mean_v6, max_v6, std_v6) = final_time_collector[key]
            w.writerow([ts, coll,"ALL-PEERS",
                        tot_v4,tot_v6, pfxs_v4, pfxs_v6, min_v4, mean_v4, max_v4, std_v4
                        , min_v6, mean_v6, max_v6, std_v6])
            
        # write out the per-time statistics
        for key in final_time:
            (ts) = key
            (tot_v4,tot_v6, pfxs_v4, pfxs_v6, min_v4, mean_v4, max_v4, std_v4
             , min_v6, mean_v6, max_v6, std_v6) = final_time[key]
            w.writerow([ts, "ALL-COLLECTORS","ALL-PEERS",
                        tot_v4,tot_v6, pfxs_v4, pfxs_v6, min_v4, mean_v4, max_v4, std_v4
                        , min_v6, mean_v6, max_v6, std_v6])

    reduced_time_collector_peer.unpersist()
    reduced_time_collector.unpersist()
    sc.stop()
    return


def main():
    parser = argparse.ArgumentParser(description="""
    Script that uses PyBGPStream and Spark to analyze historical BGP data and
    extract high-level statistics.
    """)
    parser.add_argument('-s',  '--start-time', nargs='?', required=True,
                        type=int,
                        help='Start time. (Rounded down to the nearest day.)')
    parser.add_argument('-e',  '--end-time', nargs='?', required=True,
                        type=int,
                        help='End time. (Rounded up to the nearest day.)')
    parser.add_argument('-c',  '--collector', nargs='?', required=False,
                        help='Analyze only a single collector')
    parser.add_argument('-n',  '--num-cores', nargs='?', required=False,
                        type=int,
                        help="Number of CPUs in the cluster (used to determine"
                             " how to partition the processing).")
    parser.add_argument('-m',  '--memory', nargs='?', required=False,
                        type=int,
                        help="Amount of RAM available to each worker.")
    parser.add_argument('-o',  '--outdir', nargs='?', required=False,
                        default='./',
                        help="Output directory.")
    parser.add_argument('-t',  '--data-type', nargs='?', required=True,
                        help="One of 'ribs' or 'updates'.",
                        choices=['ribs', 'updates'])
    opts = vars(parser.parse_args())
    analyze(**opts)


if __name__ == "__main__":
    main()
