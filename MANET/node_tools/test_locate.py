"""R1 geometry, contamination, provenance and ambiguity tests (no I/O)."""
from dataclasses import replace
import itertools
import math
import random
import unittest

import manet_locate as ml

FRAME = ml.LocalFrame(39.7392, -104.9903)


def anchor(name, x, y, z=1600, sigma=0.0, **kw):
    p = FRAME.position(x, y, z)
    return ml.Anchor(name, p['lat'], p['lon'], sigma, z, 0.0 if z is not None else None, **kw)


def previous(x, y, radius=1, mono=100):
    p = FRAME.position(x,y)
    return ml.Previous(p['lat'],p['lon'],radius,mono)


def observations(anchors, target=(20,30,1600), errors=None, mono=100):
    errors = errors or {}
    out=[]
    for a in anchors:
        x,y=FRAME.xy(a.lat,a.lon)
        dz=0 if a.hae is None or target[2] is None else a.hae-target[2]
        distance=math.sqrt((x-target[0])**2+(y-target[1])**2+dz**2)
        out.append(ml.Observation(a,distance+errors.get(a.id,0),mono))
    return out


def error(result, target=(20,30)):
    return math.dist(FRAME.xy(result['position']['lat'], result['position']['lon']),target[:2])


def group_pairs(points, errors=None, include=None):
    errors=errors or {}
    out=[]
    for a,b in itertools.combinations(sorted(points),2):
        if include is not None and (a,b) not in include:
            continue
        out.append(dict(a=a,b=b,range_m=math.dist(points[a],points[b])+errors.get((a,b),0),mono=100,spread_m=1,frames=50))
    return out


def marks(points, names):
    return [ml.Mark(n,**{k:v for k,v in FRAME.position(*points[n][:2]).items() if k!='hae'},h_sigma_m=0.2,mono=100) for n in names]


class AbsoluteTests(unittest.TestCase):
    def setUp(self):
        self.anchors=[anchor('A',0,0),anchor('B',100,0),anchor('C',0,100),anchor('D',100,100),anchor('E',-50,50)]

    def test_exact_three_and_five_anchors(self):
        for n in (3,5):
            r=ml.solve('P',observations(self.anchors[:n]),target_hae=1600)
            self.assertLess(error(r),0.001)
            self.assertGreater(r['uncertainty_m'],0)
            self.assertEqual(r['generation'],1)
            self.assertEqual(len(r['used']),n)
            if n==3:self.assertIn('no_outlier_redundancy',r['flags'])

    def test_weighted_uncertain_anchor_contributes_less(self):
        obs=observations(self.anchors,errors={'D':15})
        obs[3]=replace(obs[3],anchor=replace(obs[3].anchor,h_sigma_m=25))
        r=ml.solve('P',obs,target_hae=1600,max_point_radius_m=None)
        self.assertLess(error(r),2)
        self.assertLess(r['uncertainty_m'],10)
        self.assertLessEqual(error(r),r['uncertainty_m'])

    def test_one_reflection_among_four_does_not_drag_point(self):
        for bad in 'ABCD':
            r=ml.solve('P',observations(self.anchors[:4],errors={bad:45}),target_hae=1600)
            if r['position']:
                self.assertLess(error(r),5)
                self.assertIn(bad,[v['id'] for v in r['rejected']])
            else:
                self.assertEqual(r['reason'],'ambiguous')
                self.assertTrue(any(math.dist(FRAME.xy(c['position']['lat'],c['position']['lon']),(20,30))<5 for c in r['candidates']))

    def test_two_reflections_with_redundancy(self):
        aa=self.anchors+[anchor('F',50,-70),anchor('G',130,40)]
        r=ml.solve('P',observations(aa,errors={'B':40,'F':60}),target_hae=1600)
        self.assertIsNotNone(r['position'])
        self.assertLess(error(r),3)
        self.assertEqual({x['id'] for x in r['rejected']},{'B','F'})

    def test_two_reflections_with_four_anchors_not_confidently_wrong(self):
        r=ml.solve('P',observations(self.anchors[:4],errors={'B':50,'D':70}),target_hae=1600)
        if r['position']:
            self.assertLessEqual(error(r),r['uncertainty_m'])
            self.assertNotEqual(r['reason'],'good')
        else:self.assertIn(r['reason'],('ambiguous','inconsistent'))

    def test_two_reflections_can_make_a_wrong_zero_residual_fix(self):
        # A/B are 50m from both (0,0) and (0,60). Reflections on C/D make
        # *all four* observations fit the wrong point exactly. A frequency
        # prior or a residual check alone must not erase the honest branch.
        aa=[anchor('A',-40,30),anchor('B',40,30),
            anchor('C',-30,-30),anchor('D',30,-30)]
        obs=observations(aa,target=(0,0,1600),
                         errors={'C':math.hypot(30,90)-math.hypot(30,30),
                                 'D':math.hypot(30,90)-math.hypot(30,30)})
        r=ml.solve('P',obs)
        self.assertIsNone(r['position'])
        self.assertEqual(r['reason'],'ambiguous')
        points=[FRAME.xy(c['position']['lat'],c['position']['lon']) for c in r['candidates']]
        for truth in ((0,0),(0,60)):
            self.assertTrue(any(math.dist(point,truth)<.1 for point in points))

    def test_retained_collinear_trio_keeps_alternate_branch(self):
        aa=[anchor('A',0,0),anchor('B',50,0),anchor('C',100,0),
            anchor('D',0,100),anchor('E',100,100)]
        r=ml.solve('P',observations(aa,errors={'D':40,'E':60}),target_hae=1600)
        self.assertEqual(r['reason'],'ambiguous')
        self.assertIsNone(r['position'])
        self.assertTrue(any(math.dist(FRAME.xy(c['position']['lat'],c['position']['lon']),(20,30))<1
                            for c in r['candidates']))

    def test_short_noise_can_be_rejected(self):
        r=ml.solve('P',observations(self.anchors,errors={'B':-30}),target_hae=1600)
        self.assertLess(error(r),3)
        self.assertIn({'B'},[{x['id'] for x in r['rejected']}])
        self.assertEqual(r['rejected'][0]['reason'],'short_range_outlier')

    def test_plus_three_metre_common_offset(self):
        obs=observations(self.anchors,errors={a.id:3 for a in self.anchors})
        ordinary=ml.solve('P',obs,target_hae=1600)
        fitted=ml.solve('P',obs,target_hae=1600,estimate_bias=True)
        self.assertLessEqual(error(ordinary),ordinary['uncertainty_m'])
        self.assertLess(error(fitted),0.01)
        self.assertAlmostEqual(fitted['bias_m'],3,places=3)
        four=ml.solve('P',obs[:4],target_hae=1600,estimate_bias=True)
        self.assertIsNone(four['bias_m'])
        self.assertIn('bias_not_observable_or_insufficient_redundancy',four['flags'])

    def test_common_anchor_error_not_averaged_away(self):
        shifted=[replace(a,lat=FRAME.position(*[v+10 for v in FRAME.xy(a.lat,a.lon)])['lat'],
                         lon=FRAME.position(*[v+10 for v in FRAME.xy(a.lat,a.lon)])['lon'],h_sigma_m=10) for a in self.anchors]
        obs=[replace(o,anchor=a) for o,a in zip(observations(self.anchors),shifted)]
        r=ml.solve('P',obs,target_hae=1600,max_point_radius_m=None,shared_anchor_sigma_m=10)
        self.assertLessEqual(error(r),r['uncertainty_m'])
        self.assertGreater(r['uncertainty_m'],ml.CE95*10)

    def test_seeded_measured_scale_noise(self):
        rng=random.Random(881)
        covered=0
        for _ in range(40):
            bias=rng.uniform(-3,3)
            errors={a.id:bias+rng.gauss(0,1)+(rng.expovariate(1/4)+3 if rng.random()<0.08 else 0)
                    -(rng.uniform(5,15) if rng.random()<0.01 else 0) for a in self.anchors}
            r=ml.solve('P',observations(self.anchors,errors=errors),target_hae=1600)
            covered+=bool(r['position'] and error(r)<=r['uncertainty_m'])
        self.assertGreaterEqual(covered,38)

    def test_collinear_and_near_collinear_anchors(self):
        for yy in (0,0.05):
            aa=[anchor('A',0,0),anchor('B',50,yy),anchor('C',100,0),anchor('D',150,yy)]
            r=ml.solve('P',observations(aa),target_hae=1600)
            self.assertEqual(r['reason'],'poor_geometry')
            self.assertIsNone(r['position'])

    def test_two_anchor_candidates_and_previous(self):
        obs=observations(self.anchors[:2])
        r=ml.solve('P',obs,target_hae=1600)
        self.assertEqual(r['reason'],'ambiguous')
        self.assertIsNone(r['position'])
        self.assertEqual(len(r['candidates']),2)
        xy=[FRAME.xy(c['position']['lat'],c['position']['lon']) for c in r['candidates']]
        self.assertTrue(any(math.dist(p,(20,30))<0.001 for p in xy))
        self.assertTrue(any(math.dist(p,(20,-30))<0.001 for p in xy))
        selected=ml.solve('P',obs,target_hae=1600,previous=previous(20,30))
        self.assertEqual(selected['reason'],'previous_disambiguated')
        self.assertLess(error(selected),0.001)
        for p in (previous(20,0),previous(20,30,100),previous(20,30,mono=50),previous(800,800)):
            self.assertIsNone(ml.solve('P',obs,target_hae=1600,previous=p)['position'])

    def test_tangent_two_anchor_geometry(self):
        r=ml.solve('P',observations(self.anchors[:2],target=(50,0,1600)),target_hae=1600)
        self.assertEqual(r['reason'],'poor_geometry')
        self.assertEqual(r['quality'],'best_guess')
        self.assertLessEqual(error(r,(50,0)),r['uncertainty_m'])

    def test_two_anchor_radius_includes_uncertain_target_height(self):
        aa=[anchor('A',0,0,1580),anchor('B',100,0,1580)]
        obs=observations(aa)
        exact=ml.solve('P',obs,target_hae=1600)
        uncertain=ml.solve('P',obs,target_hae=1600,target_v_sigma_m=10)
        self.assertGreater(uncertain['candidates'][0]['uncertainty_m'],
                           exact['candidates'][0]['uncertainty_m'])

    def test_one_anchor_ring(self):
        r=ml.solve('P',observations(self.anchors[:1]),target_hae=1600)
        self.assertEqual(r['reason'],'ring')
        self.assertAlmostEqual(r['ring']['radius_m'],math.hypot(20,30),places=6)
        self.assertEqual(r['ring']['projection'],'circle')
        self.assertIsNone(r['position'])

    def test_missing_height_is_not_zero(self):
        aa=[replace(a,hae=None,v_sigma_m=None) for a in self.anchors[:4]]
        physical=observations(self.anchors[:4],target=(20,30,1650))
        obs=[replace(o,anchor=a) for o,a in zip(physical,aa)]
        r=ml.solve('P',obs)
        self.assertIn('unknown_height_horizontal_approximation',r['flags'])
        if r['position']:
            self.assertIsNone(r['position']['hae'])
            self.assertLessEqual(error(r),r['uncertainty_m'])
        disk=ml.solve('P',obs[:1])
        self.assertEqual(disk['ring']['projection'],'disk')
        two=ml.solve('P',obs[:2],previous=previous(20,30))
        self.assertIsNone(two['position'])

    def test_ten_metre_up_ten_metre_across(self):
        aa=[anchor('A',10,0,1610),anchor('B',0,30,1600),anchor('C',-30,-20,1620),anchor('D',20,-30,1590)]
        obs=observations(aa,target=(0,0,1600))
        self.assertAlmostEqual(obs[0].range_m,math.sqrt(200),places=6)
        r=ml.solve('P',obs,target_hae=1600)
        self.assertLess(error(r,(0,0)),0.001)

    def test_unknown_target_height_is_a_normal_usable_fix(self):
        aa=[anchor('A',-30,-20,1601,sigma=2.5),anchor('B',30,-20,1599,sigma=2.5),
            anchor('C',30,30,1602,sigma=2.5),anchor('D',-30,30,1598,sigma=2.5),
            anchor('E',0,50,1600,sigma=2.5)]
        r=ml.solve('P',observations(aa,target=(0,0,1610)))
        self.assertIsNotNone(r['position'])
        self.assertIsNone(r['position']['hae'])
        self.assertIn('anchor_height_prior',r['flags'])
        self.assertEqual(r['height_prior']['hae'],1600)
        self.assertLess(error(r,(0,0)),r['uncertainty_m'])
        self.assertLess(r['uncertainty_m'],15)
        self.assertEqual(ml.anchor_from_result('P',r).hae,None)

    def test_height_prior_is_datum_invariant_and_uses_anchor_spread(self):
        aa=[replace(a,hae=1600+i*8,v_sigma_m=2) for i,a in enumerate(self.anchors)]
        obs=observations(aa,target=(20,30,1615))
        r=ml.solve('P',obs,max_point_radius_m=None)
        shifted=[replace(o,anchor=replace(o.anchor,hae=o.anchor.hae+1200)) for o in obs]
        rr=ml.solve('P',shifted,max_point_radius_m=None)
        self.assertEqual(r['position'],rr['position'])
        self.assertAlmostEqual(r['uncertainty_m'],rr['uncertainty_m'])
        self.assertGreater(r['height_prior']['sigma_m'],ml.TERRAIN_SIGMA_M)
        self.assertEqual(rr['height_prior']['hae']-r['height_prior']['hae'],1200)

    def test_circular_radius_has_correct_isotropic_and_thin_limits(self):
        self.assertAlmostEqual(ml._circular95([[4,0],[0,4]]),2*math.sqrt(-2*math.log(.05)),places=5)
        self.assertAlmostEqual(ml._circular95([[4,0],[0,1e-12]]),2*1.95996,places=3)

    def test_ranged_chain_grows_and_preserves_transitive_ancestry(self):
        obs=observations(self.anchors[:4])
        first=ml.solve('P',obs,target_hae=1600)
        a=ml.anchor_from_result('P',first)
        second=ml.solve('Q',observations([a]+self.anchors[1:4],target=(40,35,1600)),target_hae=1600,max_point_radius_m=None)
        self.assertEqual(second['generation'],2)
        self.assertGreaterEqual(second['uncertainty_m'],first['uncertainty_m']+ml.HOP_GROWTH_M-1e-8)
        self.assertIn('A',second['used_ids'])
        q=ml.anchor_from_result('Q',second)
        third=ml.solve('R',observations([q]+self.anchors[1:4],target=(50,40,1600)),target_hae=1600,max_point_radius_m=None)
        self.assertEqual(third['generation'],3)
        cap=ml.anchor_from_result('R',third)
        result=ml.solve('S',observations([cap]+self.anchors[:3]),target_hae=1600)
        self.assertIn({'id':'R','reason':'generation_limit'},result['rejected'])
        loop=ml.solve('P',observations([q]+self.anchors[:3]),target_hae=1600)
        self.assertIn({'id':'Q','reason':'dependency_loop'},loop['rejected'])

    def test_manual_source_and_unknown_uncertainty(self):
        aa=[replace(a,source='manual',h_sigma_m=12) for a in self.anchors[:3]]
        r=ml.solve('P',observations(aa),target_hae=1600)
        self.assertEqual(set(r['sources'].values()),{'manual'})
        self.assertGreater(r['uncertainty_m'],12)
        bad=replace(aa[0],h_sigma_m=None)
        r=ml.solve('P',observations([bad]))
        self.assertIsNone(r['position'])
        self.assertTrue(r['rejected'])

    def test_independent_absolute_roots_can_improve_a_ranged_parent(self):
        aa=[replace(self.anchors[0],source='ranged',generation=1,
                    used_ids=('external-root',),h_sigma_m=15)]+self.anchors[1:]
        r=ml.solve('P',observations(aa),target_hae=1600)
        self.assertLess(error(r),.01)
        self.assertLess(r['uncertainty_m'],10)
        self.assertIn('external-root',r['used_ids'])

    def test_broad_uncertainty_is_not_a_reusable_point(self):
        aa=[replace(a,h_sigma_m=40,source='manual') for a in self.anchors]
        r=ml.solve('P',observations(aa),target_hae=1600)
        self.assertEqual(r['reason'],'coarse')
        self.assertIsNone(r['position'])
        self.assertTrue(r['candidates'])
        with self.assertRaises(ValueError):ml.anchor_from_result('P',r)
        nominal=ml.solve('P',observations(aa),target_hae=1600,max_point_radius_m=None)
        self.assertLess(error(nominal),0.001)
        self.assertEqual(nominal['uncertainty_m'],r['uncertainty_m'])

    def test_time_quality_duplicates_and_no_mutation(self):
        good=observations(self.anchors[:3])
        bad=[replace(good[0],mono=50),replace(good[1],frames=2),replace(good[2],range_m=float('nan'))]
        r=ml.solve('P',good+bad+[good[0]],target_hae=1600,at_mono=100)
        self.assertLess(error(r),0.001)
        self.assertEqual(len(r['used']),3)
        self.assertEqual(len(r['rejected']),4)
        self.assertEqual(good[0].mono,100)
        self.assertEqual(ml.solve('P',[])['reason'],'unavailable')
        future=ml.solve('P',[replace(good[0],mono=101)],at_mono=100)
        self.assertEqual(future['rejected'][0]['reason'],'stale_or_future')

    def test_motion_skew_increases_radius(self):
        obs=observations(self.anchors[:4])
        fresh=ml.solve('P',obs,target_hae=1600)
        old=ml.solve('P',obs,target_hae=1600,at_mono=105,max_speed_mps=2)
        self.assertGreaterEqual(old['uncertainty_m'],fresh['uncertainty_m']+10-1e-8)

    def test_low_frame_count_and_wide_window_reduce_precision(self):
        obs=observations(self.anchors[:4])
        normal=ml.solve('P',obs,target_hae=1600)
        few=ml.solve('P',[replace(o,frames=8) for o in obs],target_hae=1600)
        wide=ml.solve('P',[replace(o,spread_m=8) for o in obs],target_hae=1600)
        self.assertGreater(few['uncertainty_m'],normal['uncertainty_m'])
        self.assertGreater(wide['uncertainty_m'],normal['uncertainty_m'])

    def test_input_order_and_duplicate_ranges_do_not_add_information(self):
        obs=observations(self.anchors[:4])
        result=ml.solve('P',obs,target_hae=1600)
        duplicated=ml.solve('P',list(reversed(obs))+obs,target_hae=1600)
        self.assertEqual(result['position'],duplicated['position'])
        self.assertEqual(result['uncertainty_m'],duplicated['uncertainty_m'])
        self.assertEqual(result['used_ids'],duplicated['used_ids'])

    def test_invalid_ancestry_and_impossible_slant_are_rejected(self):
        bad=replace(self.anchors[0],source='ranged',generation=1,used_ids=())
        r=ml.solve('P',observations([bad]),target_hae=1600)
        self.assertEqual(r['rejected'][0]['reason'],'missing_ranged_provenance')
        high=anchor('high',0,0,1700)
        r=ml.solve('P',[ml.Observation(high,10,100)],target_hae=1600)
        self.assertEqual(r['rejected'][0]['reason'],'range_shorter_than_height')

    def test_frame_dateline_and_bounds(self):
        f=ml.LocalFrame(40,179.999)
        p=f.position(300,10)
        self.assertAlmostEqual(f.xy(p['lat'],p['lon'])[0],300,places=5)
        with self.assertRaises(ValueError):ml.LocalFrame(89,0)
        aa=self.anchors+[anchor('far',2000,0)]
        r=ml.solve('P',observations(aa),target_hae=1600)
        self.assertIn({'id':'far','reason':'outside_local_frame'},r['rejected'])


class PresentationTests(unittest.TestCase):
    def assert_drawable(self,result):
        self.assertIn(result['quality'],('good','best_guess','candidates','ring'))
        self.assertTrue(result['position'] or result['candidates'] or result['rings'] or result['area'])

    def test_close_mirrors_have_an_enclosing_best_guess(self):
        aa=[anchor('A',0,0),anchor('B',50,0)]
        result=ml.solve('P',observations(aa,target=(20,5,1600)),target_hae=1600)
        self.assertEqual(result['quality'],'best_guess')
        self.assertFalse(result['anchor_eligible'])
        p=result['position']
        xy=FRAME.xy(p['lat'],p['lon'])
        for c in result['candidates']:
            q=c['position']
            bound=math.dist(xy,FRAME.xy(q['lat'],q['lon']))+c['uncertainty_m']
            self.assertLessEqual(bound,result['uncertainty_m']+1e-6)
        with self.assertRaises(ValueError):ml.anchor_from_result('P',result)

    def test_far_mirrors_remain_drawable_above_point_limit(self):
        aa=[anchor('A',0,0),anchor('B',50,0)]
        obs=observations(aa,target=(20,30,1600))
        capped=ml.solve('P',obs,target_hae=1600)
        uncapped=ml.solve('P',obs,target_hae=1600,max_point_radius_m=None)
        self.assertEqual(capped['quality'],'candidates')
        self.assertIsNone(capped['position'])
        self.assertGreater(capped['area']['radius_m'],40)
        self.assertEqual(capped['area']['radius_m'],uncapped['uncertainty_m'])
        self.assertEqual(uncapped['quality'],'best_guess')
        self.assert_drawable(capped)

    def test_line_layout_preserves_labelled_mirror_pair(self):
        aa=[anchor(chr(65+i),i*50,0) for i in range(4)]
        result=ml.solve('P',observations(aa),target_hae=1600)
        self.assert_drawable(result)
        self.assertEqual(result['reason'],'poor_geometry')
        self.assertFalse(result['anchor_eligible'])
        labels={c['label'] for c in result['candidates']}
        self.assertTrue(any('Side A' in label for label in labels))
        self.assertTrue(any('Side B' in label for label in labels))
        for mirror in ((20,30),(20,-30)):
            self.assertTrue(any(math.dist(FRAME.xy(c['position']['lat'],c['position']['lon']),mirror)<.01
                                for c in result['candidates']))

    def test_inconsistent_and_coincident_ranges_return_areas(self):
        for aa in ([anchor('A',0,0),anchor('B',100,0),anchor('C',0,100)],
                   [anchor('A',0,0),anchor('B',0,0)]):
            result=ml.solve('P',[ml.Observation(a,1,100) for a in aa],target_hae=1600)
            self.assert_drawable(result)
            self.assertTrue(result['rings'])
            self.assertTrue(all(r['projection']=='disk' and r['outer_radius_m']>0 for r in result['rings']))
            self.assertIsNone(result['confidence'])

    def test_none_requires_no_usable_range(self):
        self.assertEqual(ml.solve('P',[])['quality'],'none')
        a=anchor('A',0,0)
        bad=ml.Observation(a,float('nan'),100)
        self.assertEqual(ml.solve('P',[bad])['quality'],'none')
        good=ml.Observation(a,30,100)
        result=ml.solve('P',[bad,good])
        self.assertEqual(result['quality'],'ring')
        self.assert_drawable(result)

    def test_conflicting_same_epoch_observations_are_order_independent(self):
        aa=[anchor('A',0,0),anchor('B',100,0),anchor('C',0,100),anchor('D',100,100)]
        obs=observations(aa)
        obs+=[replace(obs[0],range_m=obs[0].range_m+20,spread_m=8),
              replace(obs[1],range_m=obs[1].range_m+10)]
        reference=ml.solve('P',obs,target_hae=1600)
        rng=random.Random(1515)
        for _ in range(12):
            rng.shuffle(obs)
            self.assertEqual(reference,ml.solve('P',obs,target_hae=1600))


class SelectionTests(unittest.TestCase):
    def test_prefer_gnss_then_quality_and_signal(self):
        candidates=[anchor('G',20,0,sigma=2,signal_dbm=-60),anchor('M',0,20,sigma=2,source='manual'),
                    anchor('R',-20,0,sigma=2,source='ranged',generation=1,used_ids=('X',)),
                    anchor('weak',0,-20,sigma=20,signal_dbm=-95)]
        picked=ml.choose_anchors('P',candidates,max_anchors=3)
        self.assertEqual([a.id for a in picked],['G','M','R'])

    def test_bearing_spread_and_loops(self):
        candidates=[anchor('A',50,0),anchor('B',100,0),anchor('C',150,0),anchor('D',0,80),
                    anchor('loop',0,-80,source='ranged',generation=1,used_ids=('P',))]
        picked=ml.choose_anchors('P',candidates,previous=previous(0,0),max_anchors=3)
        self.assertIn('D',[a.id for a in picked])
        self.assertNotIn('loop',[a.id for a in picked])

    def test_duplicate_anchor_records_have_a_canonical_winner(self):
        aa=[anchor('A',50,0),anchor('B',0,50),anchor('C',-50,0)]
        aa.append(replace(aa[0],h_sigma_m=20,lon=aa[1].lon))
        self.assertEqual(ml.choose_anchors('P',aa),ml.choose_anchors('P',list(reversed(aa))))


class RelativeTests(unittest.TestCase):
    def setUp(self):
        self.points={'A':(0,0),'B':(100,0),'C':(10,100),'D':(80,70),'E':(-30,30)}
        self.pairs=group_pairs(self.points)

    def check_shape(self,r):
        for a,b in itertools.combinations(self.points,2):
            self.assertAlmostEqual(math.dist(r['positions'][a],r['positions'][b]),math.dist(self.points[a],self.points[b]),places=3)

    def test_no_marks_relative_and_distance_only(self):
        r=ml.relative_layout(self.pairs)
        self.assertEqual(r['quality'],'relative')
        self.check_shape(r)
        self.assertTrue(all(r['ambiguities'][k] for k in ('rotation','translation','mirror')))
        self.assertIsNone(r['absolute'])
        view=ml.distance_view('A',self.pairs)
        self.assertEqual(view[0]['label'],'B: 100 m')
        self.assertEqual(len(view),4)
        self.assertEqual(ml.distance_view('unseen',self.pairs),[])

    def test_one_mark_translation_only(self):
        r=ml.relative_layout(self.pairs,marks(self.points,'C'))
        self.assertEqual(r['quality'],'translation_only')
        self.assertEqual(r['positions']['C'],(0,0))
        self.assertFalse(r['ambiguities']['translation'])
        self.assertTrue(r['ambiguities']['rotation'])
        self.assertTrue(r['ambiguities']['mirror'])
        self.assertIsNone(r['absolute'])

    def test_two_marks_preserve_both_mirrors(self):
        r=ml.relative_layout(self.pairs,marks(self.points,'AB'))
        self.assertEqual(r['quality'],'mirror_ambiguous')
        self.assertFalse(r['ambiguities']['rotation'])
        self.assertTrue(r['ambiguities']['mirror'])
        self.assertEqual(len(r['geographic_candidates']),2)
        ys=[FRAME.xy(c['C']['lat'],c['C']['lon'])[1] for c in r['geographic_candidates']]
        self.assertAlmostEqual(max(ys),100,places=3)
        self.assertAlmostEqual(min(ys),-100,places=3)
        self.assertIsNone(r['absolute'])

    def test_three_marks_resolve_original_or_mirrored_layout(self):
        for sign in (1,-1):
            points={k:(x,sign*y) for k,(x,y) in self.points.items()}
            r=ml.relative_layout(self.pairs,marks(points,'ABC'))
            self.assertEqual(r['quality'],'anchored')
            self.assertFalse(any(r['ambiguities'].values()))
            for n,p in r['absolute'].items():
                self.assertLess(math.dist(FRAME.xy(p['lat'],p['lon']),points[n]),0.001)
            self.assertEqual(r['anchor_source'],'manual')

    def test_layout_anchors_preserve_group_dependencies(self):
        result=ml.relative_layout(self.pairs,marks(self.points,'ABC'))
        anchors=ml.anchors_from_layout(result)
        self.assertEqual(len(anchors),len(self.points))
        self.assertTrue(all(a.source=='ranged' and a.generation==1 for a in anchors))
        self.assertTrue(all('D' in a.used_ids for a in anchors))
        self.assertEqual(ml.choose_anchors('D',anchors),[])
        with self.assertRaises(ValueError):
            ml.anchors_from_layout(ml.relative_layout(self.pairs,marks(self.points,'AB')))

    def test_relative_radii_propagate_shared_radio_offsets_per_node(self):
        control=ml.relative_layout(self.pairs,marks(self.points,'ABC'),boot_sigma_m=0)
        boots=ml.relative_layout(self.pairs,marks(self.points,'ABC'))
        self.assertEqual(control['quality'],'anchored')
        for node in self.points:
            self.assertGreater(boots['uncertainty_by_id_m'][node],control['uncertainty_by_id_m'][node])
        self.assertEqual(boots['uncertainty_m'],max(boots['uncertainty_by_id_m'].values()))
        for a in ml.anchors_from_layout(boots):
            self.assertAlmostEqual(a.h_sigma_m*ml.CE95,boots['uncertainty_by_id_m'][a.id])

    def test_collinear_marks_do_not_resolve_mirror(self):
        points=dict(self.points,F=(50,0))
        r=ml.relative_layout(group_pairs(points),marks(points,'ABF'))
        self.assertEqual(r['quality'],'mirror_ambiguous')
        self.assertIsNone(r['absolute'])

    def test_bad_marks_do_not_rescale_layout(self):
        wrong=dict(self.points,B=(400,0),C=(10,400))
        r=ml.relative_layout(self.pairs,marks(wrong,'ABC'))
        self.assertEqual(r['quality'],'marks_inconsistent')
        self.assertIsNone(r['absolute'])

    def test_sparse_trilateration_graph(self):
        edges={('A','B'),('A','C'),('B','C'),('A','D'),('B','D'),('C','D'),('A','E'),('B','E'),('C','E')}
        r=ml.relative_layout(group_pairs(self.points,include=edges))
        self.assertEqual(r['quality'],'relative')
        self.check_shape(r)

    def test_flexible_disconnected_and_nonunique_graphs(self):
        r=ml.relative_layout(group_pairs(self.points,include={('A','B'),('B','C'),('C','D'),('D','E')}),marks(self.points,'ABC'))
        self.assertEqual(r['quality'],'flexible')
        self.assertIsNone(r['absolute'])
        r=ml.relative_layout(group_pairs(self.points,include={('A','B'),('C','D')}))
        self.assertEqual(r['quality'],'disconnected')
        # Triangles ABC and ABD sharing AB may reflect independently.
        r=ml.relative_layout(group_pairs(self.points,include={('A','B'),('A','C'),('B','C'),('A','D'),('B','D')}))
        self.assertIn(r['quality'],('ambiguous_geometry','flexible'))
        self.assertIsNone(r['absolute'])

    def test_collinear_range_graph_is_not_confident(self):
        points={str(i):(i*20,0) for i in range(5)}
        r=ml.relative_layout(group_pairs(points))
        self.assertIn(r['quality'],('flexible','ambiguous_geometry'))
        self.assertIsNone(r['absolute'])

    def test_relative_heights_and_inconsistent_edge(self):
        points={k:(x,y,10*i) for i,(k,(x,y)) in enumerate(self.points.items())}
        r=ml.relative_layout(group_pairs(points),heights={k:p[2] for k,p in points.items()})
        self.assertEqual(r['quality'],'relative')
        self.check_shape(r)
        self.assertNotIn('planar_height_assumption',r['flags'])
        bad=ml.relative_layout(group_pairs(self.points,errors={('A','B'):80}))
        self.assertEqual(bad['quality'],'inconsistent')
        self.assertIsNone(bad['absolute'])

    def test_mark_age_preserved_and_future_rejected(self):
        mm=[replace(m,mono=10) for m in marks(self.points,'ABC')]
        r=ml.relative_layout(self.pairs,mm)
        self.assertEqual(r['mark_ages_s'],{'A':90,'B':90,'C':90})
        rr=ml.relative_layout(self.pairs,[replace(mm[0],mono=101)])
        self.assertEqual(rr['quality'],'relative')
        self.assertEqual(rr['rejected'][0]['reason'],'invalid_mark')

    def test_sixteen_radios_exact_layout(self):
        rng=random.Random(191)
        points={f'R{i}':(rng.uniform(-100,100),rng.uniform(-100,100)) for i in range(16)}
        result=ml.relative_layout(group_pairs(points))
        self.assertEqual(result['quality'],'relative')
        for a,b in itertools.combinations(points,2):
            self.assertAlmostEqual(math.dist(points[a],points[b]),math.dist(result['positions'][a],result['positions'][b]),places=3)


if __name__=='__main__':
    unittest.main()
