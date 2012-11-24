import sys, os, time, pdb, traceback

try:
    import cPickle as pickle
except:
    import pickle

import cv

sys.path.append('..')

import barcode.sequoia as sequoia
import grouping.partask as partask, grouping.tempmatch as tempmatch

from Vendor import Vendor

class SequoiaVendor(Vendor):
    def __init__(self, proj):
        self.proj = proj

    def decode_ballots(self, ballots, manager=None, queue=None):
        flipmap, mark_bbs_map, err_imgpaths, backsmap = partask.do_partask(_decode_ballots,
                                                                           ballots,
                                                                           _args=(sequoia.ZERO_IMGPATH,
                                                                                  sequoia.ONE_IMGPATH,
                                                                                  sequoia.SIDESYM_IMGPATH),
                                                                           combfn=_combfn,
                                                                           init=({}, {}, [], {}),
                                                                           manager=manager,
                                                                           pass_queue=queue,
                                                                           N=1)
        # BACKSMAP: maps {int ballotID: [(imgpath, isflip), ...]}
        self.backsmap = backsmap
        return (flipmap, mark_bbs_map, err_imgpaths)

    def partition_ballots(self, verified_results, manual_labeled):
        """
        Input:
            dict VERIFIED_RESULTS: maps {markval, [(imgpath, (x1,y1,x2,y2), userdata), ...]}
            dict MANUAL_LABELED: maps {imgpath: [str decoding_i, ...]}
        Note: imgpaths not in VERIFIED_RESULTS but in FLIPMAP are back sides.
        Output:
            (dict PARTITIONS, dict IMG2DECODING, dict IMGINFO_MAP)
        """
        partitions = {}
        img2decoding = {}
        imginfo_map = {}
        
        decodings_tmp = {} # maps {imgpath: [(left/right, y1, digitval), ...]}
        for markval, tups in verified_results.iteritems():
            digitval = '1' if markval == sequoia.MARK_ON else '0'
            for (imgpath, bb, side) in tups:
                decodings_tmp.setdefault(imgpath, []).append((side, bb[1], digitval))
        
        for imgpath, tups in decodings_tmp.iteritems():
            # group by LEFT/RIGHT, sort by y1 coordinate
            left = [(y1, digitval) for (side, y1, digitval) in tups if side == sequoia.LEFT]
            right = [(y1, digitval) for (side, y1, digitval) in tups if side == sequoia.RIGHT]
            left_sorted = sorted(left, key=lambda t: t[0])
            right_sorted = sorted(right, key=lambda t: t[0])
            left_decoding = ''.join([val for (y1, val) in left_sorted])
            right_decoding = ''.join([val for (y1, val) in right_sorted])
            decoding = (left_decoding, right_decoding)
            img2decoding[imgpath] = decoding
            imginfo = get_imginfo(decoding)
            imginfo['page'] = 0
            imginfo_map[imgpath] = imginfo
        for imgpath, decoding in manual_labeled.iteritems():
            img2decoding[imgpath] = decoding
            imginfo_map[imgpath] = get_imginfo(decoding)
        for ballotid, backimgpaths in self.backsmap.iteritems():
            for imgpath in backimgpaths:
                # IMGPATH is a back-side image.
                imginfo = {'page': 1}
                imginfo_map[imgpath] = imginfo
                img2decoding[imgpath] = "BACK"  # Signal for backside"

        img2bal = pickle.load(open(self.proj.image_to_ballot, 'rb'))
        bal2imgs = pickle.load(open(self.proj.ballot_to_images, 'rb'))
        decoding2partition = {} # maps {(dec0, dec1, ...): int partitionID}
        curPartitionID = 0
        history = set()
        for imgpath, decoding in img2decoding.iteritems():
            ballotid = img2bal[imgpath]
            if ballotid in history:
                continue
            imgpaths = bal2imgs[ballotid]
            imgpaths_ordered = sorted(imgpaths, key=lambda imP: imginfo_map[imP]['page'])
            decodings_ordered = tuple([img2decoding[imP] for imP in imgpaths_ordered])
            partitionid = decoding2partition.get(decodings_ordered, None)
            if partitionid == None:
                decoding2partition[decodings_ordered] = curPartitionID
                partitionid = curPartitionID
                curPartitionID += 1
            partitions.setdefault(partitionid, []).append(ballotid)
            history.add(ballotid)
        
        return partitions, img2decoding, imginfo_map

    def __repr__(self):
        return 'SequoiaVendor()'
    def __str__(self):
        return 'SequoiaVendor()'

def get_imginfo(decodings):
    """ Note: barcode values are output top-to-down.
    """
    # TODO: Actually output the party/ballot-layout-id.
    return {}

def _combfn(a, b):
    flipmap_a, mark_bbs_map_a, errs_imgpaths_a, backsmap_a = a
    flipmap_b, mark_bbs_map_b, errs_imgpaths_b, backsmap_b = b
    flipmap_out = dict(flipmap_a.items() + flipmap_b.items())
    mark_bbs_map_out = mark_bbs_map_a
    for marktype, tups in mark_bbs_map_b.iteritems():
        mark_bbs_map_out.setdefault(marktype, []).extend(tups)
    errs_imgpaths_out = errs_imgpaths_a + errs_imgpaths_b
    backs_map_out = dict(backsmap_a.items() + backsmap_b.items())
    return (flipmap_out, mark_bbs_map_out, errs_imgpaths_out, backs_map_out)

def _decode_ballots(ballots, (template_path_zero, template_path_one, sidesym_path), queue=None):
    """
    Input:
        dict BALLOTS: {int ballotID: [imgpath_i, ...]}
    Output:
        (dict flipmap, dict mark_bbs, list err_imgpaths)
    Since backsides do not really have barcodes, and I detect the 
    front/back by the ISIDESYM mark, back sides are handled differently.
    If an image I is found to be a backside, it will be added to the
    FLIPMAP, but not to the MARK_BBS. 
    The SequoiaVendor object will be responsible for recognizing that
    imgpaths not present in the VERIFIED_RESULTS, but present in the
    FLIPMAP, are back-side images. 
    """
    try:
        flipmap = {}
        mark_bbs_map = {} # maps {str "ON"/"OFF": [(imgpath, (x1,y1,x2,y2), userdata), ...]}
        err_imgpaths = []
        Itemp0 = cv.LoadImage(template_path_zero, cv.CV_LOAD_IMAGE_GRAYSCALE)
        Itemp1 = cv.LoadImage(template_path_one, cv.CV_LOAD_IMAGE_GRAYSCALE)
        Isidesym = cv.LoadImage(sidesym_path, cv.CV_LOAD_IMAGE_GRAYSCALE)
        # Rescale to current image resolution
        exmpl_imgsize = cv.GetSize(cv.LoadImage(ballots.values()[0][0], cv.CV_LOAD_IMAGE_UNCHANGED))
        if exmpl_imgsize != (sequoia.ORIG_IMG_W, sequoia.ORIG_IMG_H):
            print "...rescaling template patches to match current resolution..."
            Itemp0 = sequoia.rescale_img(Itemp0, sequoia.ORIG_IMG_W, sequoia.ORIG_IMG_H,
                                         exmpl_imgsize[0], exmpl_imgsize[1])
            Itemp1 = sequoia.rescale_img(Itemp1, sequoia.ORIG_IMG_W, sequoia.ORIG_IMG_H,
                                         exmpl_imgsize[0], exmpl_imgsize[1])
            Isidesym = sequoia.rescale_img(Isidesym, sequoia.ORIG_IMG_W, sequoia.ORIG_IMG_H,
                                         exmpl_imgsize[0], exmpl_imgsize[1])
        Itemp0 = tempmatch.smooth(Itemp0, 3, 3, bordertype='const', val=255.0)
        Itemp1 = tempmatch.smooth(Itemp1, 3, 3, bordertype='const', val=255.0)
        Isidesym = tempmatch.smooth(Isidesym, 3, 3, bordertype='const', val=255.0)

        backs_map = {} # maps {int ballotid: [imgpath_i, ...]}
        for ballotid, imgpaths in ballots.iteritems():
            # 1.) First, separate front-sides from back-sides
            # FRONTS: [(imgpath, I, flip), ...]
            # BACKS: [(imgpath, flip), ...]
            fronts, backs = [], []
            # TOOD: Assumes that ballots have been scanned in pairs (i.e.
            # front/back pairs). Can only handle ballot sizes of multiples of 2.
            # Assumes that the ballot pairs are ordered (but that front/back is
            # unknown).
            for (imgpath0, imgpath1) in by_n_gen(imgpaths, 2):
                I0 = tempmatch.smooth(cv.LoadImage(imgpath0, cv.CV_LOAD_IMAGE_GRAYSCALE),
                                      3, 3, bordertype='const', val=255.0)
                I1 = tempmatch.smooth(cv.LoadImage(imgpath1, cv.CV_LOAD_IMAGE_GRAYSCALE),
                                      3, 3, bordertype='const', val=255.0)
                # Complication: We only know single-sided when given a
                # front-side image of a single-sided ballot. Also, 
                # the back-side of a single-sided ballot will return None
                # for compute_side. Sigh.
                side0, flip0, issingle0 = sequoia.compute_side(I0, Isidesym)
                side1, flip1, issingle1 = sequoia.compute_side(I1, Isidesym)
                if 'npp' in imgpath0 or 'npp' in imgpath1:
                    print 'imgpath0: {0} side0={1} flip0={2} issingle0={3}'.format(imgpath0, side0, flip0, issingle0)
                    print 'imgpath1: {0} side1={1} flip1={2} issingle1={3}'.format(imgpath1, side1, flip1, issingle1)
                    pdb.set_trace()
                if side0 == 0 and issingle0:
                    if side1 == 0:
                        # Danger: Inconsistency. 
                        err_imgpaths.append(imgpath0)
                        err_imgpaths.append(imgpath1)
                    else:
                        fronts.append((imgpath0, I0, flip0))
                        backs.append(imgpath1)
                        flipmap[imgpath1] = False # flip doesn't mattter, it's empty anyways.
                elif side1 == 0 and issingle1:
                    if side0 == 0:
                        err_imgpaths.append(imgpath1)
                        err_imgpaths.append(imgpath0)
                    else:
                        fronts.append((imgpath1, I1, flip1))
                        backs.append(imgpath0)
                        flipmap[imgpath0] = False
                else:
                    # Now that we've handled the annoying single-sided issue...
                    if side0 == None:
                        err_imgpaths.append(imgpath0)
                    if side1 == None:
                        err_imgpaths.append(imgpath1)
                    if side0 == side1 and side0 != None and side1 != None:
                        # Danger: Inconsistency
                        err_imgpaths.extend((imgpath0, imgpath1))
                        continue
                    if side0 == 0:
                        front = (imgpath0, I0, flip0)
                        back = imgpath1
                        flipmap[imgpath1] = flip1
                    else:
                        front = (imgpath1, I1, flip1)
                        back = imgpath0
                        flipmap[imgpath0] = flip0
                    fronts.append(front)
                    backs.append(back)

            backs_map[ballotid] = backs
            # 2.) Run barcode decoding on fronts
            for (imgpath, I, flip) in fronts:
                decodings, isflip, mark_locs = sequoia.decode(I, Itemp0, Itemp1, _imgpath=imgpath)
                if decodings == None:
                    err_imgpaths.append(imgpath)
                else:
                    flipmap[imgpath] = isflip
                    for marktype, tups in mark_locs.iteritems():
                        mark_bbs_map.setdefault(marktype, []).extend(tups)
                if queue: queue.put(True)
        return flipmap, mark_bbs_map, err_imgpaths, backs_map
    except:
        traceback.print_exc()
        pdb.set_trace()

def by_n_gen(seq, n):
    i = 0
    while i < len(seq):
        toreturn = seq[i:i+n]
        yield toreturn
        i += n
