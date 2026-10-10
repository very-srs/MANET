"""Pure ranging position solver (R1), using only the standard library.

Inputs are frozen Anchor, Observation, Previous and Mark records.
solve(node_id, observations, ...) returns a dict. choose_anchors returns
Anchor records. relative_layout and distance_view accept pair dicts:
{a, b, range_m, mono, spread_m=1, frames=50}. Callers supply acquisition-time
positions and monotonic timestamps in one clock domain. No clocks, files
or devices are read here.

All distances and uncertainties are metres. Anchor h_sigma_m is per-axis
horizontal standard uncertainty, v_sigma_m vertical, neither CE90 nor HDOP.
spread_m describes a completed range window, not independent frame errors.
Range noise has a 1 m floor at 50 frames, inflated by sqrt(50/n) below 50
and never reduced above 50. At least 8 frames are required. Each radio's
boot offset is shared across links, with a zero-mean Uniform(+/-2.7 m)
moment prior, not an independent 3 m margin per link.

Without target height, use the median anchor height as prior mean and
sigma = sqrt(anchor height variance + terrain_sigma_m**2). Jointly fit
height, horizontal position and common range offset. The default 5 m
terrain allowance assumes local ground. Enlarge it for cliffs/floors.
Supplied height retains target_v_sigma_m unless explicitly exact. Missing
anchor heights assume a labelled 20 m vertical spread. Inferred height is
never a GNSS fix. Free common-bias fitting needs at least five retained anchors.
Otherwise the boot-offset prior stays active.

Local WGS84 meridional/prime-vertical radii at the first anchor define EN.
All anchors and solutions must be within 1 km of that origin, |lat| <= 80
degrees. No global, geodesic or polar solving is supported.

uncertainty_m is a model-based 95% horizontal circle conditional on these
noise/terrain assumptions. It propagates independent anchor error once,
shared target bias/height through nuisance covariance, and model disagreement.
shared_anchor_sigma_m optionally adds known common horizontal error apart from
independent h_sigma_m. Long-only contamination uses a declared 20%/30 m
exponential stress prior, with neither frequency nor tail length measured.
Compatible leave-two-out modes remain a profile safeguard at low prior mass.
Arbitrary/coherent false anchors and more than two bad ranges are not covered.

quality/flags must accompany the radius. solve returns good, best_guess,
candidates, ring or none, with an estimator diagnostic in reason. The default
40 m point limit selects a point or candidates/area. None disables the limit.
Competing modes get an enclosing circle and best_guess when small enough.
Near-collinear layouts retain labelled mirror branches. Unfittable usable
ranges retain disks. Only no usable range gives none. Display-only guesses
have anchor_eligible=False. Previous estimates only select between two close,
plausible intersections, never adding independent anchors or hidden WLS priors.

Ranged anchors require transitive used_ids and propagated radius via
anchor_from_result. Union ancestry prevents feedback through chains, with
at most 3 ranging generations. Manual anchors retain source='manual', with
caller-owned mark authorization/age. Relative layouts assume coplanarity
unless heights are supplied. Gauge fixes are arbitrary. Geography requires
resolved orientation and reflection. Three marks cannot resolve a flexible
or non-unique range graph.
"""
from dataclasses import dataclass
from itertools import combinations
import math
from statistics import median

# Local distances are metres. Generations bound transitive anchor reuse.
MAX_LOCAL_M = 1000.0
MAX_GENERATION = 3
MAX_NODES = 16
# Circular 95% factor for an isotropic Gaussian with unit axis variance.
CE95 = 2.448
# Metre allowances for ring widths and dependent ranging generations.
BIAS_ALLOWANCE_M = 3.0
HOP_GROWTH_M = 3.0
BOOT_SIGMA_M = 2.7 / math.sqrt(3)  # Uniform per-radio offset, shared by its links.
TERRAIN_SIGMA_M = 5.0


@dataclass(frozen=True)
class Anchor:
    """An anchor position with uncertainty, source and transitive ancestry."""
    id: str
    lat: float
    lon: float
    h_sigma_m: float
    hae: float | None = None
    v_sigma_m: float | None = None
    source: str = "gnss"
    generation: int = 0
    used_ids: tuple[str, ...] = ()
    signal_dbm: float | None = None


@dataclass(frozen=True)
class Observation:
    """A range window and its acquisition-time anchor position."""
    anchor: Anchor
    range_m: float
    mono: float
    spread_m: float = 1.0
    frames: int = 50


@dataclass(frozen=True)
class Previous:
    """A prior point and radius for distinguishing circle branches."""
    lat: float
    lon: float
    uncertainty_m: float
    mono: float


@dataclass(frozen=True)
class Mark:
    """A caller-authorized manual position at a monotonic epoch."""
    id: str
    lat: float
    lon: float
    h_sigma_m: float
    mono: float


def _finite(x):
    """Test for a finite real number, excluding booleans."""
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _nonnegative(x):
    """Test for a finite nonnegative number."""
    return _finite(x) and x >= 0


def _position_ok(lat, lon):
    """Check coordinates against the local solver latitude limits."""
    return _finite(lat) and _finite(lon) and abs(lat) <= 80 and abs(lon) <= 180


def _anchor_reason(a, node_id):
    """Return a rejection reason for invalid or dependent anchors."""
    if not isinstance(a, Anchor) or not isinstance(a.id, str) or not a.id:
        return "invalid_anchor"
    if not _position_ok(a.lat, a.lon) or not _nonnegative(a.h_sigma_m):
        return "invalid_position_or_uncertainty"
    if a.hae is not None and not _finite(a.hae):
        return "invalid_height"
    if a.v_sigma_m is not None and not _nonnegative(a.v_sigma_m):
        return "invalid_vertical_uncertainty"
    if a.source not in ("gnss", "manual", "ranged"):
        return "invalid_source"
    if not isinstance(a.generation, int) or isinstance(a.generation, bool) or a.generation < 0:
        return "invalid_generation"
    if not isinstance(a.used_ids, (tuple, list)) or any(not isinstance(x, str) or not x for x in a.used_ids):
        return "invalid_ancestry"
    if a.id == node_id or node_id in a.used_ids:
        return "dependency_loop"
    if a.source == "ranged":
        if not a.used_ids or a.generation < 1:
            return "missing_ranged_provenance"
        if a.generation >= MAX_GENERATION:
            return "generation_limit"
    elif a.generation != 0:
        return "invalid_generation"
    if a.signal_dbm is not None and not _finite(a.signal_dbm):
        return "invalid_signal"
    return None


class LocalFrame:
    """Project to WGS84 local EN, retaining height as HAE, not relative U."""
    def __init__(self, lat, lon):
        """Set local WGS84 scales at the supplied degree coordinates."""
        if not _position_ok(lat, lon):
            raise ValueError("origin outside local frame latitude/longitude limits")
        self.lat, self.lon = lat, lon
        p = math.radians(lat)
        # WGS84 eccentricity squared and equatorial radius set local scales.
        e2 = 6.69437999014e-3
        w = math.sqrt(1 - e2 * math.sin(p) ** 2)
        self.north = 6378137.0 * (1 - e2) / w ** 3
        self.east = 6378137.0 / w * math.cos(p)

    def xy(self, lat, lon):
        """Project degree coordinates to east and north metres."""
        if not _position_ok(lat, lon):
            raise ValueError("position outside frame limits")
        # Wrap the longitude difference across the date line.
        dl = (lon - self.lon + 180) % 360 - 180
        return math.radians(dl) * self.east, math.radians(lat - self.lat) * self.north

    def position(self, x, y, hae=None):
        """Convert local metres to coordinates with the supplied HAE."""
        return {"lat": self.lat + math.degrees(y / self.north),
                "lon": (self.lon + math.degrees(x / self.east) + 180) % 360 - 180,
                "hae": hae}


def _linear(a, b):
    """Solve a small dense system with pivoting, returning None if singular."""
    n = len(b)
    m = [list(row) + [v] for row, v in zip(a, b)]
    scale = max((abs(v) for row in a for v in row), default=0)
    for i in range(n):
        # Largest-magnitude pivot limits division by a nearly zero entry.
        k = max(range(i, n), key=lambda j: abs(m[j][i]))
        if abs(m[k][i]) <= max(1e-12, scale * 1e-10):
            return None
        m[i], m[k] = m[k], m[i]
        d = m[i][i]
        for j in range(i, n + 1):
            m[i][j] /= d
        for k in range(n):
            if k != i:
                d = m[k][i]
                for j in range(i, n + 1):
                    m[k][j] -= d * m[i][j]
    return [row[-1] for row in m]


def _eigen(a):
    """Cyclic Jacobi eigensystem for a small real symmetric matrix."""
    n = len(a)
    a = [list(row) for row in a]
    v = [[float(i == j) for j in range(n)] for i in range(n)]
    # Bound Jacobi sweeps and stop when off-diagonal terms are below 1e-10.
    for _ in range(40):
        changed = False
        for p in range(n):
            for q in range(p + 1, n):
                apq = a[p][q]
                if abs(apq) < 1e-10:
                    continue
                changed = True
                angle = 0.5 * math.atan2(2 * apq, a[q][q] - a[p][p])
                c, s = math.cos(angle), math.sin(angle)
                app, aqq = a[p][p], a[q][q]
                a[p][p] = c*c*app - 2*s*c*apq + s*s*aqq
                a[q][q] = s*s*app + 2*s*c*apq + c*c*aqq
                a[p][q] = a[q][p] = 0.0
                for k in range(n):
                    if k not in (p, q):
                        akp, akq = a[k][p], a[k][q]
                        a[k][p] = a[p][k] = c*akp - s*akq
                        a[k][q] = a[q][k] = s*akp + c*akq
                    vkp, vkq = v[k][p], v[k][q]
                    v[k][p], v[k][q] = c*vkp-s*vkq, s*vkp+c*vkq
        if not changed:
            break
    # Largest eigenvalues first select the dominant embedding directions.
    order = sorted(range(n), key=lambda i: a[i][i], reverse=True)
    return [a[i][i] for i in order], [[v[k][i] for k in range(n)] for i in order]


def _normal(jac, residual, weights):
    """Build the weighted normal matrix and residual gradient."""
    n = len(jac[0])
    h = [[sum(w * row[i] * row[j] for row, w in zip(jac, weights))
          for j in range(n)] for i in range(n)]
    g = [sum(w * row[i] * r for row, w, r in zip(jac, weights, residual)) for i in range(n)]
    return h, g


def _predict(p, row, bias=False):
    """Return slant range with nuisance offsets and its Jacobian."""
    dx, dy = p[0] - row["x"], p[1] - row["y"]
    # Parameters are east, north, optional height correction, then bias.
    height = bool(row.get("z_sigma", 0) and len(p)>2)
    dz = row["dz"] + (p[2] if height else 0)
    bi = 3 if height else 2
    has_bias = len(p)>bi
    d = max(1e-8, math.sqrt(dx*dx + dy*dy + dz*dz))
    return d + (p[bi] if has_bias else 0), ([dx/d, dy/d]
            + ([dz/d] if height else []) + ([1.0] if has_bias else []))


def _priors(rows, bias):
    """Select target height and boot-offset priors for the fit."""
    priors = []
    if rows[0].get("z_sigma", 0):
        priors.append((2, rows[0]["z_sigma"]))
    if not bias:
        priors.append((2+bool(rows[0].get("z_sigma", 0)), BOOT_SIGMA_M))
    return priors


def _fit(rows, seed, bias=False):
    # Target height and its common boot offset are nuisance parameters. The
    # latter has a measured-scale prior unless explicitly fitting a free bias.
    """Fit a position and nuisance offsets, returning cost and information."""
    p = list(seed[:2]) + ([0.0] if rows[0].get("z_sigma", 0) else []) + [0.0]
    priors = _priors(rows, bias)
    weights = [1 / r["sigma"]**2 for r in rows]
    def cost(point):
        """Sum weighted range residuals and nuisance-prior penalties."""
        return (sum(w * (_predict(point, r, bias)[0] - r["r"])**2 for r, w in zip(rows, weights))
                + sum((point[i]/s)**2 for i,s in priors))
    # Cap refinement at 50 steps, with backtracking on increasing cost.
    for _ in range(50):
        pred = [_predict(p, r, bias) for r in rows]
        h, g = _normal([x[1] for x in pred], [x[0]-r["r"] for x,r in zip(pred, rows)], weights)
        for i,s in priors:
            h[i][i] += 1/s**2
            g[i] += p[i]/s**2
        # Damping permits refinement near an anchor, not a rank claim.
        for i in range(len(p)):
            h[i][i] += 1e-7
        step = _linear(h, [-x for x in g])
        if step is None:
            break
        old = cost(p)
        gain = 1.0
        # Try at most 12 step sizes. Free bias is limited to +/-6 metres.
        for _ in range(12):
            trial = [a + gain*b for a,b in zip(p,step)]
            if bias:
                trial[-1] = max(-6.0, min(6.0, trial[-1]))
            if cost(trial) <= old:
                break
            gain *= 0.5
        else:
            break
        p = trial
        if max(abs(gain*x) for x in step) < 1e-5:
            break
    pred = [_predict(p, r, bias) for r in rows]
    h, _ = _normal([x[1] for x in pred], [0]*len(rows), weights)
    for i,s in priors:
        h[i][i] += 1/s**2
    return p, cost(p), h


def _seed(rows):
    # Squared-range differences supply an initialization, never the final fit.
    """Initialize from range differences or the anchor centroid."""
    r0 = rows[0]
    a, b = [], []
    for r in rows[1:]:
        a.append([2*(r["x"]-r0["x"]), 2*(r["y"]-r0["y"])])
        b.append(r0["r"]**2-r["r"]**2-r0["dz"]**2+r["dz"]**2
                 +r["x"]**2+r["y"]**2-r0["x"]**2-r0["y"]**2)
    h,g = _normal(a,b,[1]*len(a))
    return _linear(h,g) or [sum(r["x"] for r in rows)/len(rows), sum(r["y"] for r in rows)/len(rows)]


def _geometry_bad(rows):
    """Detect coincident or nearly collinear anchor coordinates."""
    x = sum(r["x"] for r in rows)/len(rows)
    y = sum(r["y"] for r in rows)/len(rows)
    xx = sum((r["x"]-x)**2 for r in rows)
    yy = sum((r["y"]-y)**2 for r in rows)
    xy = sum((r["x"]-x)*(r["y"]-y) for r in rows)
    tr = xx+yy
    lo = (tr-math.hypot(xx-yy,2*xy))/2
    # Reject squared spread below 1 m^2 or a weak axis below 0.25% of it.
    return tr < 1 or lo/max(tr,1e-12) < 0.0025


def _circle_seeds(rows):
    """Both branches, especially when a retained trio is almost collinear."""
    # The widest baseline defines the two horizontal circle branches.
    a,b = max(combinations(rows,2),key=lambda pair:math.hypot(pair[0]["x"]-pair[1]["x"],pair[0]["y"]-pair[1]["y"]))
    dx,dy = b["x"]-a["x"],b["y"]-a["y"]
    d = math.hypot(dx,dy)
    if d<1e-6:
        return []
    ra2,rb2 = max(0,a["r"]**2-a["dz"]**2),max(0,b["r"]**2-b["dz"]**2)
    along = (ra2-rb2+d*d)/(2*d)
    h2 = ra2-along*along
    if h2<0:
        return []
    h = math.sqrt(h2)
    x,y = a["x"]+along*dx/d,a["y"]+along*dy/d
    return [[x-h*dy/d,y+h*dx/d],[x+h*dy/d,y-h*dx/d]]


def _covariance(h):
    """Invert an information matrix, returning None if singular."""
    cols = [_linear(h, [float(i==j) for i in range(len(h))]) for j in range(len(h))]
    if any(c is None for c in cols):
        return None
    return cols


def _circular95(cov):
    # Integrate the radial Gaussian tail over angle for a 95% circle,
    # including anisotropic geometry.
    """Approximate a Gaussian horizontal 95% containment radius."""
    xx,yy,xy = cov[0][0],cov[1][1],cov[0][1]
    major = (xx+yy+math.hypot(xx-yy,2*xy))/2
    minor = max(1e-12,xx+yy-major)
    if major<=0 or not math.isfinite(major):
        return None
    # Sample 32 angular midpoints and bisect the 5% tail for 25 steps.
    v = [major*math.cos(math.pi*(i+.5)/32)**2 + minor*math.sin(math.pi*(i+.5)/32)**2 for i in range(32)]
    lo,hi = 0.0,CE95*math.sqrt(major)
    for _ in range(25):
        mid = (lo+hi)/2
        if sum(math.exp(-mid*mid/(2*x)) for x in v)/len(v)>.05:
            lo=mid
        else:
            hi=mid
    return hi


def _radius(rows, point, cost, h, bias):
    """Combine fitted uncertainty, shared error and displacement bounds."""
    cov = _covariance(h)
    if cov is None:
        return None
    shared = rows[0].get("shared_sigma", 0)**2
    cov[0][0] += shared
    cov[1][1] += shared
    radius = _circular95(cov)
    if radius is None:
        return None
    # Motion is bounded displacement, not another independent measurement.
    radius += max(r["motion"] for r in rows)
    for r in rows:
        # Fresh absolute roots outside this parent's ancestry add information.
        # Chains with only that ancestry retain the hop-growth floor to avoid
        # averaging away parent uncertainty.
        independent_root=any(other["anchor"].source in ("gnss","manual")
                             and other["anchor"].id not in r["anchor"].used_ids
                             for other in rows if other is not r)
        if r["anchor"].source == "ranged" and not independent_root:
            radius = max(radius, CE95*r["anchor"].h_sigma_m + HOP_GROWTH_M)
    return radius


def _model_score(rows, kept, point, cost, info):
    # Mixture: Gaussian LOS and long-only exponential reflection.
    # 20%/30 m are declared stress priors, not measured occurrence statistics.
    # A tiny short-contamination branch detects gross bad data.
    """Score a retained subset against a range-contamination mixture."""
    score = cost
    for i,r in enumerate(rows):
        if i in kept:
            score += 2*math.log(math.sqrt(2*math.pi)*r["sigma"]/.8)
        else:
            residual = _predict(point,r)[0]-r["r"]
            if residual>0:
                score += 2*math.log(30/.002) + 2*residual/10
            else:
                score += 2*math.log(30/.198) - 2*residual/30
    return score


def _mixture_radius(models, point):
    # Deterministic polar quadrature of the Gaussian mixture, including model
    # disagreement. No maximum over all leave-out fits (most are improbable).
    """Find a 95% radius around a point across weighted fit modes."""
    weighted = []
    total = sum(m["weight"] for m in models)
    # Use 24 radial quantiles and 32 angles per Gaussian component.
    for m in models:
        cov=m["cov"]
        a=math.sqrt(max(1e-12,cov[0][0]))
        b=cov[0][1]/a
        c=math.sqrt(max(1e-12,cov[1][1]-b*b))
        for j in range(24):
            radius=math.sqrt(-2*math.log(1-(j+.5)/24))
            for k in range(32):
                angle=2*math.pi*(k+.5)/32
                u,v=radius*math.cos(angle),radius*math.sin(angle)
                d=math.hypot(m["p"][0]+a*u-point[0],m["p"][1]+b*u+c*v-point[1])
                weighted.append((d,m["weight"]/total/(24*32)))
    cumulative=0.0
    # Accumulate mass by distance from the chosen display point.
    for distance,w in sorted(weighted):
        cumulative+=w
        if cumulative>=.95:
            return distance
    return weighted[-1][0]


def _radius_budget(rows, model):
    """Return diagnostic component circles, adding covariances, not radii."""
    cov=model["cov"]
    # Model covariance includes caller-declared common horizontal error.
    # Exclude that error from gains due to independent observations.
    cov=[list(c) for c in cov]
    cov[0][0]-=rows[0]["shared_sigma"]**2
    cov[1][1]-=rows[0]["shared_sigma"]**2
    gain=[]
    for row in rows:
        jac=_predict(model["p"],row)[1]
        gain.append([sum(cov[k][j]*jac[j] for j in range(len(jac)))/row["sigma"]**2 for k in range(2)])
    parts={}
    for name in ("window_noise","anchor_horizontal","peer_boot","anchor_vertical","height_curvature"):
        block=[[sum(g[i]*g[j]*row["variance_parts"][name] for g,row in zip(gain,rows)) for j in range(2)] for i in range(2)]
        parts[name]=_circular95(block) or 0.0
    for i,s in _priors(rows,model["bias"]):
        name="target_height" if rows[0]["z_sigma"] and i==2 else "target_boot"
        block=[[cov[k][i]*cov[l][i]/s**2 for l in range(2)] for k in range(2)]
        parts[name]=_circular95(block) or 0.0
    return parts


def _base_result(mono, rejected):
    """Build the empty estimator result with rejection details."""
    return {"position": None, "uncertainty_m": None, "confidence": None,
            "quality": "unavailable", "used": [], "rejected": rejected,
            "sources": {}, "generation": None, "used_ids": [], "mono": mono,
            "bias_m": None, "candidates": [], "ring": None, "flags": []}


def _anchor_key(a):
    # Canonical tie break for duplicate reports, including optional fields.
    """Order duplicate anchors by quality and canonical content."""
    return (a.h_sigma_m, a.generation, a.source, a.lat, a.lon,
            repr((a.hae,a.v_sigma_m,tuple(sorted(a.used_ids)),a.signal_dbm)))


def _observation_key(o):
    """Order duplicate observations by recency, quality and content."""
    # Newest epoch wins, then the window variance per frame and content.
    return (-o.mono, max(1.0,o.spread_m)**2/max(1,o.frames),
            _anchor_key(o.anchor), o.range_m, o.frames, o.spread_m)


def _provenance(out, rows):
    """Attach retained sources and their transitive dependency union."""
    out["used"] = sorted(r["anchor"].id for r in rows)
    out["sources"] = {r["anchor"].id:r["anchor"].source for r in rows}
    out["generation"] = 1 + max(r["anchor"].generation for r in rows)
    out["used_ids"] = sorted({x for r in rows for x in (r["anchor"].id,*r["anchor"].used_ids)})


def _previous(previous, frame, mono, max_age, speed):
    """Project a fresh previous point and expand its motion bound."""
    if not isinstance(previous, Previous) or not _position_ok(previous.lat,previous.lon) \
            or not _nonnegative(previous.uncertainty_m) or not _finite(previous.mono):
        return None
    age = mono-previous.mono
    if age < 0 or age > max_age:
        return None
    return (*frame.xy(previous.lat,previous.lon),previous.uncertainty_m+speed*age)


def _solve(node_id, observations, *, previous=None, target_hae=None,
          target_v_sigma_m=0.0, estimate_bias=False, at_mono=None,
          max_age_s=15.0, max_speed_mps=1.5, max_point_radius_m=40.0,
          shared_anchor_sigma_m=0.0, terrain_sigma_m=TERRAIN_SIGMA_M):
    """Estimate horizontal position with height/bias nuisance parameters.

    See the module contract. Compare observation times to at_mono, defaulting
    to the newest input epoch. Reject ages above max_age_s and future times.
    Motion allowance bounds target displacement to at_mono. Anchor positions
    must match their range acquisition times. Keep only the newest report per
    ID, breaking ties by quality and canonical content independently of order.
    Invalid observations return reasons. Invalid options raise ValueError.
    At most 16 distinct anchors participate.
    """
    if not isinstance(node_id,str) or not node_id:
        raise ValueError("node_id must be nonempty")
    if not all(_nonnegative(x) for x in (target_v_sigma_m,max_age_s,max_speed_mps,
                                       shared_anchor_sigma_m,terrain_sigma_m)):
        raise ValueError("invalid uncertainty/age/speed option")
    if target_hae is not None and not _finite(target_hae):
        raise ValueError("invalid target height")
    if max_point_radius_m is not None and (not _finite(max_point_radius_m) or max_point_radius_m<=0):
        raise ValueError("invalid point radius limit")
    observations = list(observations)
    if at_mono is None:
        at_mono = max((o.mono for o in observations if isinstance(o,Observation) and _finite(o.mono)),default=0)
    if not _finite(at_mono):
        raise ValueError("invalid at_mono")
    rejected, unique = [], {}
    for o in observations:
        aid = getattr(getattr(o,"anchor",None),"id",None)
        reason = _anchor_reason(o.anchor,node_id) if isinstance(o,Observation) else "invalid_observation"
        if not reason and (not _nonnegative(o.range_m) or o.range_m > MAX_LOCAL_M*2
                           or not _nonnegative(o.spread_m) or not _finite(o.mono)
                           or not isinstance(o.frames,int) or isinstance(o.frames,bool) or o.frames < 8):
            reason = "invalid_range_quality"
        if not reason and (at_mono-o.mono > max_age_s or o.mono > at_mono):
            reason = "stale_or_future"
        if reason:
            rejected.append({"id":aid,"reason":reason})
            continue
        if aid in unique:
            rejected.append({"id":aid,"reason":"duplicate_observation"})
            if _observation_key(unique[aid]) <= _observation_key(o):
                continue
        unique[aid] = o
    out = _base_result(at_mono,rejected)
    out["vertical_sigma_m"] = target_v_sigma_m if target_hae is not None else None
    if not unique:
        return out
    if len(unique) > MAX_NODES:
        raise ValueError("at most 16 distinct anchors; use choose_anchors first")
    # Canonical ID order fixes the origin and floating-point traversal order.
    obs = sorted(unique.values(),key=lambda o:o.anchor.id)
    frame = LocalFrame(obs[0].anchor.lat,obs[0].anchor.lon)
    heights = [o.anchor.hae for o in obs if o.anchor.hae is not None
               and o.anchor.v_sigma_m is not None]
    height_mean = target_hae if target_hae is not None else median(heights) if heights else 0.0
    height_sigma = target_v_sigma_m if target_hae is not None else math.sqrt(
        terrain_sigma_m**2 + sum((z-height_mean)**2 for z in heights)/max(1,len(heights)))
    if target_hae is None:
        out["flags"].append("anchor_height_prior" if heights else "unknown_height_horizontal_approximation")
        out["height_prior"] = {"hae":height_mean if heights else None,"sigma_m":height_sigma}
    rows = []
    for o in obs:
        a = o.anchor
        x,y = frame.xy(a.lat,a.lon)
        if math.hypot(x,y) > MAX_LOCAL_M:
            rejected.append({"id":a.id,"reason":"outside_local_frame"})
            continue
        unknown = a.hae is None or a.v_sigma_m is None
        dz = 0.0 if unknown else height_mean-a.hae
        motion = max_speed_mps*(at_mono-o.mono)
        # Window noise has a 1 m floor and gains no precision past 50 frames.
        range_sigma = max(1.0,o.spread_m)*math.sqrt(max(1.0,50/o.frames))
        # Unknown anchor heights contribute at least 20 m standard error.
        vertical_var = max(20.0,height_sigma)**2 if unknown else a.v_sigma_m**2
        vertical_projection = min(1.0,abs(dz)/max(o.range_m,1))
        # Independent anchor errors enter range variance. Target height and
        # common target bias remain shared fit parameters.
        variance_parts={"window_noise":range_sigma**2,"peer_boot":BOOT_SIGMA_M**2,
                        "anchor_horizontal":a.h_sigma_m**2,"anchor_vertical":vertical_projection**2*vertical_var,
                        "height_curvature":(vertical_var+height_sigma**2)**2/(2*max(o.range_m,1)**2)}
        sigma = math.sqrt(sum(variance_parts.values()))
        if abs(dz) > o.range_m + 3*sigma+BIAS_ALLOWANCE_M:
            rejected.append({"id":a.id,"reason":"range_shorter_than_height"})
            continue
        rows.append({"anchor":a,"x":x,"y":y,"dz":dz,"r":o.range_m,
                     "sigma":sigma,"unknown_height":unknown,"motion":motion,
                     "z_sigma":height_sigma,"shared_sigma":shared_anchor_sigma_m,
                     "variance_parts":variance_parts})
    if not rows:
        return out
    out["_rows"],out["_frame"]=rows,frame
    _provenance(out,rows)
    if any(r["unknown_height"] for r in rows) and "unknown_height_horizontal_approximation" not in out["flags"]:
        out["flags"].append("unknown_height_horizontal_approximation")
    prev = _previous(previous,frame,at_mono,max_age_s,max_speed_mps)
    if len(rows) <= 2:
        _few_anchors(out,rows,frame,target_hae,prev)
        _point_limit(out,max_point_radius_m)
        return out
    if _geometry_bad(rows):
        out["quality"] = "poor_geometry"
        return out
    # Each leave-out set is a contamination model. Keep near-collinear trio
    # branches separate without giving all fitted subsets equal weight.
    models = []
    for drop_count in range(min(2,len(rows)-2 if len(rows)>=4 else 0)+1):
        for dropped in combinations(range(len(rows)),drop_count):
            indices = [i for i in range(len(rows)) if i not in dropped]
            subset = [rows[i] for i in indices]
            # A free common bias needs five retained anchors.
            use_bias = bool(estimate_bias and len(subset)>=5)
            seeds = [_seed(subset)] + (_circle_seeds(subset) if len(subset)<=3 else [])
            branches = []
            for seed in seeds:
                point,cost,info = _fit(subset,seed,use_bias)
                if math.hypot(*point[:2])>MAX_LOCAL_M:
                    continue
                cov = _covariance(info)
                radius = _radius(subset,point,cost,info,use_bias)
                if cov is None or radius is None:
                    continue
                residuals = [_predict(point,r)[0]-r["r"] for r in rows]
                # Retained ranges allow the larger of 6 m and four sigma.
                if any(abs(residuals[i])>max(6,4*rows[i]["sigma"]) for i in indices):
                    continue
                score = _model_score(rows,indices,point,cost,info)
                model = dict(p=point,cost=cost,score=score,cov=cov,radius=radius,
                             kept=indices,bias=use_bias,residuals=residuals)
                # Seeds converging within 1 m represent the same local mode.
                near = next((j for j,m in enumerate(branches) if math.dist(m["p"][:2],point[:2])<1),None)
                if near is None:
                    branches.append(model)
                elif score<branches[near]["score"]:
                    branches[near]=model
            # Splitting one subset into two modes must not double its mass.
            for model in branches:
                model["score"] += 2*math.log(len(branches))
            models.extend(branches)
    if not models:
        out["quality"] = "inconsistent"
        return out
    # Score ranks models, with retained indices and parameters breaking ties.
    models.sort(key=lambda m:(m["score"],tuple(m["kept"]),tuple(m["p"])))
    best=models[0]
    # Relative likelihoods avoid exponentiating the full absolute score.
    for model in models:
        model["weight"]=math.exp(-.5*(model["score"]-best["score"]))
        model["cov"][0][0] += shared_anchor_sigma_m**2
        model["cov"][1][1] += shared_anchor_sigma_m**2
    total=sum(m["weight"] for m in models)
    p,radius,inliers=best["p"],best["radius"],best["kept"]
    residuals=best["residuals"]
    kept=[rows[i] for i in inliers]
    _provenance(out,kept)
    for i,r in enumerate(rows):
        if i not in inliers:
            rejected.append({"id":r["anchor"].id,"reason":"long_range_outlier" if residuals[i]<0 else "short_range_outlier",
                             "residual_m":residuals[i]})
    out["candidates"]=[{"position":frame.position(*m["p"][:2],target_hae),
                        "uncertainty_m":m["radius"],"probability":m["weight"]/total} for m in models]
    # Integrate modes within 18 score units of the best fit.
    mixture=_mixture_radius([m for m in models if m["score"]-best["score"]<18],p)
    # Keep compatible long-only explanations as a profile set despite low
    # contamination prior mass, especially for two honest circles among four.
    profile=[m for m in models if all(m["residuals"][i]<=2*rows[i]["sigma"] for i in range(len(rows)) if i not in m["kept"])
             and m["cost"]<=max(3.84,3.84*(len(m["kept"])-2))]
    out["_plausible"]=[{"position":frame.position(*m["p"][:2],target_hae),
                        "uncertainty_m":m["radius"],"probability":m["weight"]/total}
                       for m in models if m is best or any(m is alt for alt in profile)]
    # A subset's weak-axis covariance omits excluded ranges' upper bounds.
    # Use compatible modes' spread. Within-mode uncertainty is integrated above.
    motion=max(r["motion"] for r in kept)
    profile_radius=max((math.hypot(math.dist(p[:2],m["p"][:2]),radius-motion)+motion for m in profile),default=radius)
    out["uncertainty_m"]=max(radius,mixture+motion,profile_radius)
    out["radius_components_m"]={"within_model":radius-motion,"model_mixture":mixture,
                                "profile_alternatives":profile_radius,
                                "motion_bound":motion,"shared_anchor_sigma":shared_anchor_sigma_m}
    out["radius_components_m"].update(_radius_budget(kept,best))
    out["confidence"]=.95
    out["bias_m"]=p[-1] if best["bias"] else None
    out["quality"]="good" if len(inliers)==len(rows) and len(rows)>=4 else "degraded"
    if len(rows)==3:
        out["flags"].append("no_outlier_redundancy")
    if estimate_bias and not best["bias"]:
        out["flags"].append("bias_not_observable_or_insufficient_redundancy")
    if best["bias"] and abs(p[-1])>=5.99:
        out["flags"].append("bias_at_limit")
        out["quality"]="degraded"
    if any(r["unknown_height"] for r in kept):
        out["quality"]="horizontal_only"
    ambiguous=any(math.dist(p[:2],m["p"][:2])>max(20,2*radius) for m in profile)
    if ambiguous or _geometry_bad(kept):
        out["quality"]="ambiguous"
    else:
        out["position"]=frame.position(*p[:2],target_hae)
    out["residual_rms_m"]=math.sqrt(sum(residuals[i]**2 for i in inliers)/len(inliers))

    _point_limit(out,max_point_radius_m)
    return out


def _range_rings(rows, frame, hae, *, inconsistent=False):
    """Project usable slant ranges into horizontal rings or disks."""
    rings=[]
    for row in rows:
        margin=CE95*row["sigma"]+BIAS_ALLOWANCE_M+row["motion"]
        nominal=math.sqrt(max(0,row["r"]**2-row["dz"]**2))
        outer=math.sqrt(max(0,(row["r"]+margin)**2-row["dz"]**2))
        disk=inconsistent or row["unknown_height"]
        inner=0.0 if disk else math.sqrt(max(0,max(0,row["r"]-margin)**2-row["dz"]**2))
        rings.append({"center":frame.position(row["x"],row["y"],hae),
                      "radius_m":nominal,"width_m":max(outer-nominal,nominal-inner),
                      "inner_radius_m":inner,"outer_radius_m":outer,
                      "projection":"disk" if disk else "circle",
                      "anchor_id":row["anchor"].id,"label":"Range from "+row["anchor"].id})
    return rings


def _line_candidates(rows, frame, hae):
    """Fit along-line position and squared cross-line distance with mirrors.

    Squared-distance covariance stays finite where the XY Jacobian is
    singular. Circles enclose the 95% marginal box after conversion to
    distance, including its curved edge. This local line approximation
    is labelled and cannot be reused as an anchor.
    """
    weights=[1/r["sigma"]**2 for r in rows]
    total=sum(weights)
    centre=[sum(w*r[k] for r,w in zip(rows,weights))/total for k in ("x","y")]
    scatter=[[sum(w*(r[a]-centre[i])*(r[b]-centre[j]) for r,w in zip(rows,weights))
              for j,b in enumerate(("x","y"))] for i,a in enumerate(("x","y"))]
    vals,vectors=_eigen(scatter)
    if vals[0]<1e-6:
        return []
    ux,uy=vectors[0]
    # Fix eigenvector sign so the two side labels remain deterministic.
    if ux<0 or (ux==0 and uy<0):ux,uy=-ux,-uy
    nx,ny=-uy,ux
    projected=[]
    for row in rows:
        dx,dy=row["x"]-centre[0],row["y"]-centre[1]
        along=dx*ux+dy*uy
        off=dx*nx+dy*ny
        projected.append(dict(row,x=centre[0]+along*ux,y=centre[1]+along*uy,
                              sigma=math.hypot(row["sigma"],off)))
    models=[]
    for count in range(min(2,len(rows)-2)+1):
        for dropped in combinations(range(len(rows)),count):
            kept=[i for i in range(len(rows)) if i not in dropped]
            subset=[projected[i] for i in kept]
            seeds=_circle_seeds(subset)
            # Start off the anchor line when nominal circles do not meet.
            if not seeds:
                height=max(5.0,sum(r["r"] for r in subset)/len(subset)/4)
                seed=_seed(subset)
                seeds=[[seed[0]+height*nx,seed[1]+height*ny]]
            fits=[_fit(subset,seed) for seed in seeds]
            point,cost,_=min(fits,key=lambda f:(f[1],tuple(f[0])))
            if math.hypot(*point[:2])>MAX_LOCAL_M:
                continue
            residuals=[_predict(point,r)[0]-r["r"] for r in projected]
            if any(abs(residuals[i])>max(6,4*projected[i]["sigma"]) for i in kept):
                continue
            jac=[]
            for row in subset:
                pred,j=_predict(point,row)
                distance=max(1e-8,pred-point[-1])
                # Differentiate by along-line position and squared cross-line
                # distance to retain information at a tangent.
                jac.append([j[0]*ux+j[1]*uy,1/(2*distance)]+j[2:])
            h,_=_normal(jac,[0]*len(jac),[1/r["sigma"]**2 for r in subset])
            for i,sigma in _priors(subset,False):h[i][i]+=1/sigma**2
            cov=_covariance(h)
            if cov is None or cov[0][0]<0 or cov[1][1]<0:
                continue
            along=(point[0]-centre[0])*ux+(point[1]-centre[1])*uy
            cross=abs((point[0]-centre[0])*nx+(point[1]-centre[1])*ny)
            t_margin=CE95*math.sqrt(cov[1][1])
            cross_margin=max(math.sqrt(cross*cross+t_margin)-cross,
                             cross-math.sqrt(max(0,cross*cross-t_margin)))
            radius=math.hypot(CE95*math.sqrt(cov[0][0]),cross_margin)
            radius+=CE95*rows[0]["shared_sigma"]+max(r["motion"] for r in subset)
            plausible=cost<=max(3.84,3.84*(len(kept)-2)) and all(residuals[i]<=2*projected[i]["sigma"] for i in dropped)
            score=_model_score(projected,kept,point,cost,h)
            models.append((score,along,cross,radius,plausible))
    if not models:
        return []
    models.sort()
    candidates=[]
    for index,(_,along,cross,radius,plausible) in enumerate(models):
        if index and not plausible:continue
        for sign,label in ((1,"Side A of anchor line"),(-1,"Side B of anchor line")):
            pos=frame.position(centre[0]+along*ux+sign*cross*nx,
                               centre[1]+along*uy+sign*cross*ny,hae)
            candidates.append({"position":pos,"uncertainty_m":radius,"label":label})
    return candidates


def _compact_candidates(candidates, frame):
    """Merge coincident candidate points using the largest radius."""
    result=[]
    for candidate in candidates:
        pos=candidate["position"]
        xy=frame.xy(pos["lat"],pos["lon"])
        # Merge points within one centimetre without shrinking their radius.
        near=next((c for c in result if math.dist(xy,frame.xy(c["position"]["lat"],c["position"]["lon"]))<.01),None)
        if near is None:
            result.append(dict(candidate))
        else:
            if candidate.get("uncertainty_m") is not None:
                near["uncertainty_m"]=max(near.get("uncertainty_m") or 0,candidate["uncertainty_m"])
    for i,candidate in enumerate(result):
        candidate.setdefault("label","Best fit" if i==0 else "Alternative "+str(i))
    return result


def _display_result(out, limit, hae):
    """Convert estimator diagnostics into drawable points and areas."""
    rows=out.pop("_rows",[])
    frame=out.pop("_frame",None)
    plausible=out.pop("_plausible",out["candidates"])
    reason=out["quality"]
    # Canonical rejection order keeps output stable under input permutations.
    out["rejected"].sort(key=lambda r:(str(r.get("id","")),r["reason"],repr(r.get("residual_m"))))
    out["reason"]=reason
    out["anchor_eligible"]=bool(out["position"] and reason not in ("horizontal_only","poor_geometry","ambiguous"))
    out["area"]=None
    out["rings"]=[]
    if not rows:
        out["quality"]="none"
        return out
    if reason=="ring":
        out["rings"]=_range_rings(rows,frame,hae)
        out["ring"]=out["rings"][0]
        return out
    if out["position"] is not None:
        out["quality"]="good" if reason=="good" else "best_guess"
        out["candidates"]=[{"position":dict(out["position"]),"uncertainty_m":out["uncertainty_m"],"label":"Estimate"}]
        return out
    if reason=="coarse":
        # The estimator already enclosed its competing modes. Changing the
        # representation at the point limit must not change that calibration.
        plausible=[dict(out["candidates"][0],uncertainty_m=out["uncertainty_m"])]
    if reason=="poor_geometry":
        plausible=_line_candidates(rows,frame,hae)
        out["flags"].append("anchor_line_mirror_ambiguity")
    candidates=_compact_candidates(plausible,frame)
    # Inconsistent or coincident ranges still carry drawable information.
    if not candidates or any(not _nonnegative(c.get("uncertainty_m")) for c in candidates):
        out["quality"]="ring" if len(rows)==1 else "candidates"
        out["candidates"]=[]
        out["rings"]=_range_rings(rows,frame,hae,inconsistent=True)
        out["ring"]=out["rings"][0] if len(rows)==1 else None
        out["confidence"]=None
        return out
    best=candidates[0]["position"]
    origin=frame.xy(best["lat"],best["lon"])
    # Each candidate contributes its distance from the best point plus radius.
    enclosing=max(math.dist(origin,frame.xy(c["position"]["lat"],c["position"]["lon"]))+c["uncertainty_m"] for c in candidates)
    enclosing=max(enclosing,out["uncertainty_m"] or 0)
    out["candidates"]=candidates
    out["area"]={"center":dict(best),"radius_m":enclosing,"label":"Possible position area"}
    out["uncertainty_m"]=enclosing
    out["confidence"]=.95
    out["quality"]="candidates"
    if limit is None or enclosing<=limit:
        out["position"]=dict(best)
        out["quality"]="best_guess"
    else:
        if "uncertainty_exceeds_point_limit" not in out["flags"]:
            out["flags"].append("uncertainty_exceeds_point_limit")
    return out


def solve(node_id, observations, *, previous=None, target_hae=None,
          target_v_sigma_m=0.0, estimate_bias=False, at_mono=None,
          max_age_s=15.0, max_speed_mps=1.5, max_point_radius_m=40.0,
          shared_anchor_sigma_m=0.0, terrain_sigma_m=TERRAIN_SIGMA_M):
    """Return drawable geometry and good/best_guess/candidates/ring/none.

    Points enclose all plausible candidate circles. Above max_point_radius_m,
    retain candidates and an enclosing area. Near-collinear anchors retain
    mirror branches. Inconsistent ranges retain per-anchor disks. none means
    no usable range survived validation. reason carries the estimator diagnostic
    independently of display quality. anchor_eligible permits estimate reuse.
    Relative-layout geometry statuses use a separate API.
    """
    out=_solve(node_id,observations,previous=previous,target_hae=target_hae,
               target_v_sigma_m=target_v_sigma_m,estimate_bias=estimate_bias,
               at_mono=at_mono,max_age_s=max_age_s,max_speed_mps=max_speed_mps,
               max_point_radius_m=max_point_radius_m,
               shared_anchor_sigma_m=shared_anchor_sigma_m,terrain_sigma_m=terrain_sigma_m)
    return _display_result(out,max_point_radius_m,target_hae)


def _point_limit(out, limit):
    """Withhold a point whose radius exceeds the display limit."""
    if out["position"] is not None and limit is not None and out["uncertainty_m"]>limit:
        out["position"]=None
        out["quality"]="coarse"
        out["flags"].append("uncertainty_exceeds_point_limit")


def _few_anchors(out,rows,frame,hae,previous):
    """Build a ring or circle intersections from one or two anchors."""
    radii = [math.sqrt(max(0,r["r"]**2-r["dz"]**2)) for r in rows]
    if len(rows)==1:
        r = rows[0]
        margin = CE95*r["sigma"]+BIAS_ALLOWANCE_M
        outer = math.sqrt(max(0,(r["r"]+margin)**2-r["dz"]**2))
        inner = math.sqrt(max(0,max(0,r["r"]-margin)**2-r["dz"]**2))
        out["quality"] = "ring"
        out["ring"] = {"center":frame.position(r["x"],r["y"],r["anchor"].hae),
                       "radius_m":radii[0],"width_m":max(outer-radii[0],radii[0]-inner),
                       "projection":"disk" if r["unknown_height"] else "circle"}
        return
    a,b = rows
    dx,dy = b["x"]-a["x"],b["y"]-a["y"]
    d = math.hypot(dx,dy)
    if d<1e-6:
        out["quality"] = "poor_geometry"
        return
    along = (radii[0]**2-radii[1]**2+d*d)/(2*d)
    # Negative squared cross-baseline distance means the circles do not meet.
    h2 = radii[0]**2-along**2
    if h2 < -1e-6:
        out["quality"] = "inconsistent"
        return
    height = math.sqrt(max(0,h2))
    x,y = a["x"]+along*dx/d,a["y"]+along*dy/d
    points = [(x-height*dy/d,y+height*dx/d),(x+height*dy/d,y-height*dx/d)]
    # Include height/common-offset nuisances as in the >=3-anchor fit.
    # The nominal circle branch has zero residual. Only covariance changes.
    nominal,cost,h = _fit(rows,points[0])
    uncertainty = _radius(rows,nominal,cost,h,False)
    if uncertainty is None:
        out["quality"] = "poor_geometry"
        out["flags"].append("tangent_circles")
        out["candidates"] = [{"position":frame.position(*p,hae),"uncertainty_m":None} for p in points]
        return
    # Half the mirror separation below the radius is near-tangent geometry.
    if height<uncertainty:
        out["quality"] = "poor_geometry"
        out["flags"].append("near_tangent_circles")
    else:
        out["quality"] = "ambiguous"
    if any(r["unknown_height"] for r in rows):
        uncertainty = max(uncertainty,2*min(radii)+CE95*max(r["sigma"] for r in rows))
    out["candidates"] = [{"position":frame.position(*p,hae),"uncertainty_m":uncertainty} for p in points]
    if previous is not None and out["quality"]!="poor_geometry" and not out["flags"]:
        distances = [math.dist(p,previous[:2]) for p in points]
        near = min(range(2),key=distances.__getitem__)
        reach = previous[2]+uncertainty
        # A previous point selects only a reachable, well-separated branch.
        if distances[near]<=reach and abs(distances[0]-distances[1])>2*reach:
            out.update(position=frame.position(*points[near],hae),uncertainty_m=uncertainty,
                       confidence=0.95,quality="previous_disambiguated")
            out["flags"].append("previous_selected_branch")


def anchor_from_result(node_id, result, signal_dbm=None):
    """Preserve radius and transitive provenance when reusing a point as anchor."""
    p = result.get("position")
    if not p or not result.get("anchor_eligible",False):
        raise ValueError("result is not an unambiguous anchor")
    return Anchor(node_id,p["lat"],p["lon"],result["uncertainty_m"]/CE95,
                  p.get("hae"),result.get("vertical_sigma_m"),"ranged",result["generation"],
                  tuple(result["used_ids"]),signal_dbm)


def choose_anchors(node_id, candidates, *, previous=None, max_anchors=5):
    """Choose up to 3..5 anchors, balancing bearing diversity and quality.

    Use the previous position or, on cold start, the observable anchor centroid
    as the angular origin. Source weights prefer GNSS, manual, then ranged,
    without certifying authenticity.
    """
    if max_anchors not in (3,4,5):
        raise ValueError("max_anchors must be 3, 4 or 5")
    pool = {}
    for a in candidates:
        if not _anchor_reason(a,node_id):
            if a.id not in pool or _anchor_key(a)<_anchor_key(pool[a.id]):
                pool[a.id]=a
    if len(pool)>MAX_NODES:
        raise ValueError("at most 16 candidate anchors")
    def weight(a):
        """Combine source, signal and horizontal uncertainty weights."""
        source = {"gnss":1.0,"manual":0.65,"ranged":0.35}[a.source]
        # Signal weight spans 0.1 to 1, saturating at -70 dBm.
        signal = 1 if a.signal_dbm is None else max(0.1,min(1.0,(a.signal_dbm+100)/30))
        return source*signal/(1+a.h_sigma_m**2)
    remaining = sorted(pool.values(),key=lambda a:(-weight(a),a.id))
    selected = []
    frame = LocalFrame(previous.lat,previous.lon) if isinstance(previous,Previous) and _position_ok(previous.lat,previous.lon) else None
    if frame is None and remaining:
        origin=LocalFrame(remaining[0].lat,remaining[0].lon)
        points=[origin.xy(a.lat,a.lon) for a in remaining]
        centre=origin.position(*(sum(p[k] for p in points)/len(points) for k in range(2)))
        frame=LocalFrame(centre["lat"],centre["lon"])
    h = [[0.0,0.0],[0.0,0.0]]
    while remaining and len(selected)<max_anchors:
        def score(a):
            """Score the information added by a candidate bearing."""
            if frame is None or not selected:
                return weight(a)
            x,y = frame.xy(a.lat,a.lon)
            norm = max(math.hypot(x,y),1e-6)
            x,y = x/norm,y/norm
            w = weight(a)
            xx,yy,xy = h[0][0]+w*x*x,h[1][1]+w*y*y,h[0][1]+w*x*y
            # Determinant rewards bearing diversity. Trace also rewards
            # information while the selected bearings are still collinear.
            return xx*yy-xy*xy + 0.01*(xx+yy)
        a = max(remaining,key=score)
        remaining.remove(a)
        selected.append(a)
        if frame:
            x,y = frame.xy(a.lat,a.lon)
            norm = max(math.hypot(x,y),1e-6)
            x,y,w = x/norm,y/norm,weight(a)
            h[0][0]+=w*x*x
            h[1][1]+=w*y*y
            h[0][1]+=w*x*y
    return selected


def _pair_rows(ranges, at_mono, max_age_s, heights):
    """Validate, project and deduplicate pair ranges at one epoch."""
    ranges = list(ranges)
    if at_mono is None:
        at_mono = max((r.get("mono",0) for r in ranges if isinstance(r,dict) and _finite(r.get("mono"))),default=0)
    if not _finite(at_mono) or not _nonnegative(max_age_s):
        raise ValueError("invalid observation time/age")
    unique, rejected = {}, []
    for r in ranges:
        if not isinstance(r,dict):
            rejected.append({"pair":None,"reason":"invalid_pair"})
            continue
        a,b = r.get("a"),r.get("b")
        reason = None
        if not isinstance(a,str) or not a or not isinstance(b,str) or not b or a==b:
            reason = "invalid_pair"
        elif not _nonnegative(r.get("range_m")) or not _finite(r.get("mono")) \
                or not _nonnegative(r.get("spread_m",1)) \
                or not isinstance(r.get("frames",50),int) or isinstance(r.get("frames",50),bool) or r.get("frames",50)<8:
            reason = "invalid_range_quality"
        elif r["mono"]>at_mono or at_mono-r["mono"]>max_age_s:
            reason = "stale_or_future"
        if reason:
            rejected.append({"pair":[a,b],"reason":reason})
            continue
        # Reciprocal measurements share one undirected pair key.
        key = tuple(sorted((a,b)))
        distance = r["range_m"]
        if a in heights and b in heights:
            dh = heights[a]-heights[b]
            if abs(dh)>distance:
                rejected.append({"pair":list(key),"reason":"range_shorter_than_height"})
                continue
            # Known endpoint heights convert slant to horizontal distance.
            distance = math.sqrt(max(0,distance**2-dh**2))
        row = {"a":key[0],"b":key[1],"d":distance,"slant_m":r["range_m"],
               "sigma":max(1.0,r.get("spread_m",1))*math.sqrt(max(1.0,50/r.get("frames",50))),"mono":r["mono"]}
        if key in unique:
            rejected.append({"pair":list(key),"reason":"duplicate_pair"})
            # Newest pair wins, then lower uncertainty and distance.
            if (-unique[key]["mono"],unique[key]["sigma"],unique[key]["d"])<=(-row["mono"],row["sigma"],row["d"]):
                continue
        unique[key]=row
    return sorted(unique.values(),key=lambda r:(r["a"],r["b"])),rejected,at_mono


def distance_view(node_id, ranges, *, at_mono=None, max_age_s=15):
    """Return direct observed distances without graph paths or positions."""
    rows,_,now = _pair_rows(ranges,at_mono,max_age_s,{})
    return [{"id":r["b"] if r["a"]==node_id else r["a"],
             "range_m":r["slant_m"],"age_s":now-r["mono"],
             "label":f"{r['b'] if r['a']==node_id else r['a']}: {r['slant_m']:.0f} m"}
            for r in rows if node_id in (r["a"],r["b"])]


def _embed(ids, edges):
    """Shortest-path MDS initialization, followed by measured-edge WLS only."""
    n=len(ids)
    ix={name:i for i,name in enumerate(ids)}
    d=[[0.0 if i==j else math.inf for j in range(n)] for i in range(n)]
    for r in edges:
        i,j=ix[r["a"]],ix[r["b"]]
        d[i][j]=d[j][i]=r["d"]
    # Complete missing distances with shortest paths for initialization.
    for k in range(n):
        for i in range(n):
            for j in range(n):
                d[i][j]=min(d[i][j],d[i][k]+d[k][j])
    # Double-center squared distances and keep the two dominant axes.
    means=[sum(x*x for x in row)/n for row in d]
    total=sum(means)/n
    gram=[[-0.5*(d[i][j]**2-means[i]-means[j]+total) for j in range(n)] for i in range(n)]
    vals,vecs=_eigen(gram)
    xy=[[vecs[k][i]*math.sqrt(max(0,vals[k])) for k in range(2)] for i in range(n)]
    # Gauge: node 0 at zero, the most distant node on the positive x axis.
    root=xy[0][:]
    xy=[[x-root[0],y-root[1]] for x,y in xy]
    second=max(range(1,n),key=lambda i:math.hypot(*xy[i]))
    angle=math.atan2(xy[second][1],xy[second][0])
    c,s=math.cos(angle),math.sin(angle)
    xy=[[c*x+s*y,-s*x+c*y] for x,y in xy]
    # Remove two translations and one rotation from the fit variables.
    variables=[(i,k) for i in range(1,n) for k in range(2) if not(i==second and k==1)]
    def evaluate(points):
        """Build measured-edge residuals and derivatives for free axes."""
        residual,jac,weights=[],[],[]
        for r in edges:
            i,j=ix[r["a"]],ix[r["b"]]
            dx,dy=points[i][0]-points[j][0],points[i][1]-points[j][1]
            length=max(math.hypot(dx,dy),1e-8)
            residual.append(length-r["d"])
            jac.append([((dx,dy)[k]/length)*(1 if v==i else -1 if v==j else 0) for v,k in variables])
            weights.append(1/r["sigma"]**2)
        return residual,jac,weights
    # Limit refinement to 60 iterations with at most 12 backtracking trials.
    for _ in range(60):
        res,jac,w=evaluate(xy)
        h,g=_normal(jac,res,w)
        for i in range(len(h)):
            h[i][i]+=1e-5
        delta=_linear(h,[-v for v in g])
        if delta is None:
            break
        gain=1.0
        old=sum(a*a*b for a,b in zip(res,w))
        for _ in range(12):
            trial=[p[:] for p in xy]
            for (i,k),v in zip(variables,delta):
                trial[i][k]+=gain*v
            rr,_,_=evaluate(trial)
            if sum(a*a*b for a,b in zip(rr,w))<=old:
                break
            gain*=0.5
        else:
            break
        xy=trial
        if max(abs(gain*v) for v in delta)<1e-5:
            break
    res,jac,w=evaluate(xy)
    h,_=_normal(jac,res,w)
    eigen,_=_eigen(h)
    # Undamped information rank tests local rigidity after gauge removal.
    rank=sum(v>max(1e-7,eigen[0]*1e-7) for v in eigen)
    # Certify global uniqueness for graphs that grow from a triangle
    # by non-collinear trilateration.
    connected={tuple(sorted((r["a"],r["b"]))) for r in edges}
    by_id=dict(zip(ids,xy))
    def triangle_good(tri):
        """Check baseline length and normalized triangle area."""
        a,b,c=(by_id[t] for t in tri)
        area=abs((b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0]))
        span=max(math.dist(a,b),math.dist(b,c),math.dist(a,c))
        # Require a baseline above 1 m and twice-area/span squared above 2%.
        return span>1 and area/span**2>0.02
    globally_rigid=False
    for tri in combinations(ids,3):
        if not all(tuple(sorted(pair)) in connected for pair in combinations(tri,2)) or not triangle_good(tri):
            continue
        located=set(tri)
        changed=True
        while changed:
            changed=False
            for node in ids:
                if node in located:
                    continue
                neighbors=[v for v in sorted(located) if tuple(sorted((node,v))) in connected]
                if any(triangle_good(t) for t in combinations(neighbors,3)):
                    located.add(node)
                    changed=True
        if len(located)==n:
            globally_rigid=True
            break
    rms=math.sqrt(sum(r*r for r in res)/len(res))
    # Per-node blocks, not the weakest mode of the whole 2N-dimensional
    # system. This preliminary radius is only for validating manual alignment.
    cov=_covariance(h) if rank==len(variables) else None
    radius=None
    if cov:
        lookup={key:i for i,key in enumerate(variables)}
        blocks=[]
        for node in range(n):
            def entry(k,l):
                """Read one node covariance entry, with zero for fixed axes."""
                i,j=lookup.get((node,k)),lookup.get((node,l))
                return cov[i][j] if i is not None and j is not None else 0.0
            blocks.append(_circular95([[entry(0,0),entry(0,1)],[entry(1,0),entry(1,1)]]) or 0)
        radius=max(blocks)
    return by_id,rms,rank==2*n-3,globally_rigid,radius,res


def _mark_transform(xy, marks, frame, mirror):
    # Fit rigid 2D alignment without scaling. Marks must agree with ranges.
    """Fit a weighted rigid mark alignment for one reflection."""
    source=[(xy[m.id][0],(-1 if mirror else 1)*xy[m.id][1]) for m in marks]
    target=[frame.xy(m.lat,m.lon) for m in marks]
    weights=[1/max(1,m.h_sigma_m)**2 for m in marks]
    total=sum(weights)
    pc=[sum(w*p[k] for w,p in zip(weights,source))/total for k in range(2)]
    qc=[sum(w*p[k] for w,p in zip(weights,target))/total for k in range(2)]
    dot=cross=0.0
    for w,p,q in zip(weights,source,target):
        x,y=p[0]-pc[0],p[1]-pc[1]
        u,v=q[0]-qc[0],q[1]-qc[1]
        dot+=w*(x*u+y*v)
        cross+=w*(x*v-y*u)
    theta=math.atan2(cross,dot)
    c,s=math.cos(theta),math.sin(theta)
    result={}
    for node,(x,y) in xy.items():
        y*=(-1 if mirror else 1)
        x,y=x-pc[0],y-pc[1]
        result[node]=(qc[0]+c*x-s*y,qc[1]+s*x+c*y)
    errors=[math.dist(result[m.id],q) for m,q in zip(marks,target)]
    return result,math.sqrt(sum(w*e*e for w,e in zip(weights,errors))/total),qc


def _placed_layout(xy, edges, marks, frame, boot_sigma):
    """Joint range/mark refinement and sandwich covariance for shared offsets."""
    ids=sorted(xy)
    ix={node:i for i,node in enumerate(ids)}
    p=[v for node in ids for v in xy[node]]
    targets={m.id:frame.xy(m.lat,m.lon) for m in marks}
    def evaluate(p):
        """Build range and mark residuals with radio endpoint IDs."""
        jac,res,variances,ends=[],[],[],[]
        for e in edges:
            a,b=ix[e["a"]],ix[e["b"]]
            dx,dy=p[2*a]-p[2*b],p[2*a+1]-p[2*b+1]
            d=max(1e-8,math.hypot(dx,dy))
            row=[0.0]*len(p)
            row[2*a],row[2*a+1],row[2*b],row[2*b+1]=dx/d,dy/d,-dx/d,-dy/d
            jac.append(row);res.append(d-e["d"])
            variances.append(e["sigma"]**2);ends.append((a,b))
        for m in marks:
            for k in range(2):
                j=2*ix[m.id]+k
                row=[0.0]*len(p);row[j]=1.0
                jac.append(row);res.append(p[j]-targets[m.id][k])
                variances.append(max(.01,m.h_sigma_m**2));ends.append(())
        # Each range has two radio offsets. Manual mark rows have none.
        weights=[1/(v+len(end)*boot_sigma**2) for v,end in zip(variances,ends)]
        return jac,res,variances,ends,weights
    # Cap joint refinement at 20 iterations and backtrack increasing cost.
    for _ in range(20):
        jac,res,variances,ends,w=evaluate(p)
        h,g=_normal(jac,res,w)
        delta=_linear(h,[-v for v in g])
        if delta is None:
            return None,None
        old=sum(v*v*weight for v,weight in zip(res,w))
        gain=1.0
        for _ in range(12):
            trial=[a+gain*b for a,b in zip(p,delta)]
            _,rr,_,_,_=evaluate(trial)
            if sum(v*v*weight for v,weight in zip(rr,w))<=old:
                break
            gain*=.5
        else:
            break
        p=trial
        if max(abs(gain*v) for v in delta)<1e-5:
            break
    jac,res,variances,ends,w=evaluate(p)
    h,_=_normal(jac,res,w)
    inv=_covariance(h)
    if inv is None:
        return None,None
    # Sum independent observation gains for all links incident on a radio
    # to obtain the gain of its shared boot error.
    gain=[[sum(inv[i][k]*row[k] for k in range(len(p)))*weight for row,weight in zip(jac,w)] for i in range(len(p))]
    bg=[[sum(gain[i][j] for j,end in enumerate(ends) if node in end) for node in range(len(ids))] for i in range(len(p))]
    def covariance(i,k):
        """Combine independent noise and shared radio-offset gains."""
        return (sum(a*b*v for a,b,v in zip(gain[i],gain[k],variances))
                + boot_sigma**2*sum(a*b for a,b in zip(bg[i],bg[k])))
    radii={}
    for node,i in ix.items():
        radii[node]=_circular95([[covariance(2*i,2*i),covariance(2*i,2*i+1)],
                                [covariance(2*i+1,2*i),covariance(2*i+1,2*i+1)]])
    return {node:p[2*i:2*i+2] for node,i in ix.items()},radii


def relative_layout(ranges, marks=(), *, at_mono=None, max_age_s=15, heights=None,
                    boot_sigma_m=BOOT_SIGMA_M):
    """Fit a relative planar layout, optionally placed with manual marks.

    Return arbitrary local (x,y) metres. absolute stays None with fewer than
    three marks or unresolved geometry. Two marks give geographic_candidates
    reflected about their baseline. Three well-separated, non-collinear,
    consistent marks resolve the reflection. Marks are snapshot constraints:
    report mono/age without renewal. Callers decide if old marks fit a moving
    group. Optional heights maps node IDs to known HAE in metres.
    Report inconsistent edges without removing them because sparse graphs
    cannot safely identify bad ranges. boot_sigma_m propagates one shared
    radio offset through all incident links. Zero requires calibrated/no-offset
    inputs. Placed layouts return uncertainty_by_id_m and its maximum,
    uncertainty_m.
    """
    heights={} if heights is None else dict(heights)
    if any(not _finite(v) for v in heights.values()) or not _nonnegative(boot_sigma_m):
        raise ValueError("invalid relative height")
    edges,rejected,now=_pair_rows(ranges,at_mono,max_age_s,heights)
    ids=sorted({n for e in edges for n in (e["a"],e["b"])})
    if len(ids)>MAX_NODES:
        raise ValueError("at most 16 radios")
    out={"quality":"unavailable","positions":{},"absolute":None,
         "geographic_candidates":[],"uncertainty_m":None,"mono":now,
         "ambiguities":{"translation":True,"rotation":True,"mirror":True,"flexible":False,"other_embeddings":False},
         "rejected":rejected,"marks_used":[],"mark_ages_s":{},
         "flags":[] if all(n in heights for n in ids) else ["planar_height_assumption"]}
    if not ids:
        return out
    neighbors={n:set() for n in ids}
    for e in edges:
        neighbors[e["a"]].add(e["b"])
        neighbors[e["b"]].add(e["a"])
    # Disconnected components have no shared relative placement.
    seen={ids[0]}
    pending=[ids[0]]
    while pending:
        for n in neighbors[pending.pop()]-seen:
            seen.add(n)
            pending.append(n)
    if len(seen)!=len(ids):
        out["quality"]="disconnected"
        out["ambiguities"]["flexible"]=True
        return out
    if len(ids)==2:
        xy={ids[0]:(0.0,0.0),ids[1]:(edges[0]["d"],0.0)}
        rms,rigid,global_ok,radius,res=0.0,True,True,CE95*edges[0]["sigma"]+3,[0]
    else:
        xy,rms,rigid,global_ok,radius,res=_embed(ids,edges)
    out.update(positions=xy,residual_rms_m=rms,uncertainty_m=radius)
    out["ambiguities"].update(flexible=not rigid,other_embeddings=not global_ok)
    if not rigid:
        out["quality"]="flexible"
        return out
    if not global_ok:
        out["quality"]="ambiguous_geometry"
        return out
    # Flag residuals above the larger of 4 m and three sigma, including
    # both radio offsets in the variance.
    bad=[e for e,r in zip(edges,res) if abs(r)>max(4,3*math.sqrt(e["sigma"]**2+2*boot_sigma_m**2))]
    if bad:
        out["quality"]="inconsistent"
        out["rejected"].extend({"pair":[e["a"],e["b"]],"reason":"layout_residual"} for e in bad)
        return out
    if max(math.dist(a,b) for a,b in combinations(xy.values(),2))>MAX_LOCAL_M:
        out["quality"]="outside_local_frame"
        return out
    out["quality"]="relative"
    unique={}
    for m in marks:
        if not isinstance(m,Mark) or m.id not in xy or not _position_ok(m.lat,m.lon) or not _nonnegative(m.h_sigma_m) or not _finite(m.mono) or m.mono>now:
            out["rejected"].append({"mark":getattr(m,"id",None),"reason":"invalid_mark"})
            continue
        # Newest mark wins, then lower uncertainty and coordinate order.
        if m.id not in unique or (-m.mono,m.h_sigma_m,m.lat,m.lon)<(-unique[m.id].mono,unique[m.id].h_sigma_m,unique[m.id].lat,unique[m.id].lon):
            unique[m.id]=m
    marks=sorted(unique.values(),key=lambda m:m.id)
    if not marks:
        return out
    out["marks_used"]=[m.id for m in marks]
    out["mark_ages_s"]={m.id:now-m.mono for m in marks}
    out["ambiguities"]["translation"]=False
    frame=LocalFrame(marks[0].lat,marks[0].lon)
    if len(marks)==1:
        x,y=xy[marks[0].id]
        out["positions"]={n:(p[0]-x,p[1]-y) for n,p in xy.items()}
        out["origin"]={"lat":marks[0].lat,"lon":marks[0].lon,"hae":None}
        out["quality"]="translation_only"
        return out
    targets=[frame.xy(m.lat,m.lon) for m in marks]
    span=max(math.dist(a,b) for a,b in combinations(targets,2))
    mark_sigma=max(m.h_sigma_m for m in marks)
    if span<max(1,3*mark_sigma):
        out["quality"]="marks_poor_geometry"
        return out
    transforms=[_mark_transform(xy,marks,frame,mirror) for mirror in (False,True)]
    # Rank the two reflections by weighted mark-alignment RMS.
    transforms.sort(key=lambda t:t[1])
    best,other=transforms
    tolerance=radius+CE95*mark_sigma
    if best[1]>tolerance or any(math.hypot(*p)>MAX_LOCAL_M for p in best[0].values()):
        out["quality"]="marks_inconsistent"
        return out
    out["ambiguities"]["rotation"]=False
    candidates=[{n:frame.position(*p,heights.get(n)) for n,p in t[0].items()} for t in transforms]
    out["geographic_candidates"]=candidates
    spread=[]
    for tri in combinations(targets,3):
        a,b,c=tri
        spread.append(abs((b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0]))/span**2)
    # Resolve reflection only with spread marks and a clear fit margin.
    if len(marks)<3 or max(spread,default=0)<0.02 or other[1]-best[1]<=2*tolerance:
        out["quality"]="mirror_ambiguous"
        return out
    out["ambiguities"]["mirror"]=False
    placed,radii=_placed_layout(best[0],edges,marks,frame,boot_sigma_m)
    if placed is None:
        out["quality"]="marks_poor_geometry"
        return out
    out["absolute"]={n:frame.position(*p,heights.get(n)) for n,p in placed.items()}
    out["geographic_candidates"]=[out["absolute"]]
    out["quality"]="anchored"
    out["uncertainty_by_id_m"]=radii
    out["uncertainty_m"]=max(radii.values())
    out["confidence"]=0.95
    out["source"]="ranged"
    out["anchor_source"]="manual"
    out["generation"]=1
    out["used_ids"]=ids
    return out


def anchors_from_layout(result):
    """Export a fully placed layout with shared group ancestry.

    Every anchor depends on the whole group and cannot feed back as an
    independent anchor for its members. Supplied heights in relative_layout
    are exact, absent heights unknown. Use solve for uncertain-height anchors.
    """
    if result.get("quality")!="anchored" or not result.get("absolute"):
        raise ValueError("layout orientation and mirror must be resolved")
    return [Anchor(n,p["lat"],p["lon"],result.get("uncertainty_by_id_m",{}).get(n,result["uncertainty_m"])/CE95,
                   p["hae"],0.0 if p["hae"] is not None else None,"ranged",
                   result["generation"],tuple(result["used_ids"]))
            for n,p in sorted(result["absolute"].items())]
