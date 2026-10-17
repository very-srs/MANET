"""Bounded majority ACS agreement with authenticated I/O in the runner.

A coordinator proposes one immutable plan per round. Votes bind the complete
plan, membership and activation time. This is partition-local agreement, not
atomic delivery or a global consensus protocol across disconnected meshes.
DFS contracts require unanimous live receiver leases and a shared fallback.
"""
from copy import deepcopy
import hashlib
import json
import re
from manet_rendezvous import RECOVERY_SECONDS

# Round offsets, vote deadlines and activation margins are in seconds.
ROUND_SECONDS = 180
PROPOSE_FROM = 45  # Allow the :25 readiness requests to replicate first.
PROPOSE_UNTIL = 60
VOTE_TIMEOUT = 60
ACTIVATION_LEAD = 30
COMMIT_CUTOFF = 5
APPLY_GRACE = 5
MAX_MEMBERS = 64
# Candidate frequencies are MHz, keyed by conventional Wi-Fi band.
CHANNELS = {'2.4': {2437, 2462}, '5': {5200, 5220, 5240, 5745, 5765, 5785, 5805, 5825}}
# DFS v1 describes only 20 MHz primaries. Actual PHY support is also required.
DFS_CHANNELS = set(range(5260, 5340, 20)) | set(range(5500, 5740, 20))
DFS_VERSION = 1
DFS_LEASE_SECONDS = 15
DFS_COOLDOWN_SECONDS = 1800
# Membership uses lowercase MACs and 32-hex boot/nonce tokens.
MAC = re.compile(r'(?:[0-9a-f]{2}:){5}[0-9a-f]{2}')
TOKEN = re.compile(r'[0-9a-f]{32}')


class AgreementError(ValueError):
    """Reject an invalid participant view or channel proposal."""
    pass


def digest(plan):
    """Hash canonical proposal JSON so votes bind the complete plan."""
    return hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(',', ':'),
                                    allow_nan=False).encode()).hexdigest()


def snapshot(status):
    """Capture the boot and current channels used to guard a promise."""
    return {'boot': status['boot'], 'current': status['current']}


def valid_status(status):
    """Check the boot token, readiness flags and per-band frequencies."""
    if not isinstance(status, dict):
        return False
    if not isinstance(status.get('boot'), str) or not TOKEN.fullmatch(status['boot']):
        return False
    if type(status.get('acs')) is not bool or type(status.get('ready')) is not bool:
        return False
    current, allowed = status.get('current'), status.get('allowed')
    if not isinstance(current, dict) or not isinstance(allowed, dict) or set(current) != set(allowed):
        return False
    for band, freq in current.items():
        # Static configurations need not be in the ACS candidate set.
        if band not in CHANNELS or type(freq) is not int or not (2400 <= freq < 2500 if band == '2.4' else 5000 <= freq < 5900):
            return False
        if not isinstance(allowed[band], list) or any(type(f) is not int or f not in CHANNELS[band] for f in allowed[band]):
            return False
    return True


def validate_view(view):
    """Reject malformed membership, status records or oversized views."""
    if (not isinstance(view, dict) or not 1 <= len(view) <= MAX_MEMBERS
            or any(not isinstance(mac, str) or not MAC.fullmatch(mac)
                   or (status is not None and not valid_status(status)) for mac, status in view.items())):
        raise AgreementError('invalid participant view')


def coordinator(view):
    """Choose the lowest ready ACS MAC with a current Wi-Fi band."""
    candidates = [mac for mac, s in view.items() if s and s['acs'] and s['ready'] and s['current']]
    return min(candidates) if candidates else None


def majority(plan):
    """Return the quorum, requiring every participant for DFS."""
    if 'dfs' in plan:
        return len(plan['participants'])
    return len(plan['participants']) // 2 + 1


def make_plan(view, channels, limp, now, nonce, fallback=None):
    """Build and validate a fixed-deadline proposal from the current view."""
    validate_view(view)
    leader = coordinator(view)
    if leader is None:
        raise AgreementError('no ready ACS coordinator')
    # Unknown status keeps membership but leaves the boot token unbound.
    plan = {'round': now // ROUND_SECONDS, 'created': now,
            'deadline': now + VOTE_TIMEOUT, 'activate_at': now + VOTE_TIMEOUT + ACTIVATION_LEAD,
            'nonce': nonce, 'leader': leader, 'channels': channels, 'limp': limp,
            'moving': any(s and any(s['current'].get(b, f) != f for b, f in channels.items())
                          for s in view.values()),
            'participants': {mac: s['boot'] if s else None for mac, s in sorted(view.items())}}
    if channels.get('5') in DFS_CHANNELS:
        plan['dfs'] = dfs_contract(view, channels['5'], fallback, now)
    validate_plan(plan)
    return plan


def validate_plan(plan):
    """Validate exact fields, timing, membership and target frequencies."""
    keys = {'round', 'created', 'deadline', 'activate_at', 'nonce', 'leader', 'channels', 'limp', 'moving', 'participants'}
    if not isinstance(plan, dict) or set(plan) not in (keys, keys | {'dfs'}):
        raise AgreementError('invalid proposal fields')
    if any(type(plan[k]) is not int or plan[k] < 0 for k in ('round', 'created', 'deadline', 'activate_at')):
        raise AgreementError('invalid proposal times')
    # Creation is limited to seconds 45 through 60 of a 180-second round.
    if (not PROPOSE_FROM <= plan['created'] % ROUND_SECONDS <= PROPOSE_UNTIL
            or plan['created'] // ROUND_SECONDS != plan['round']
            or plan['deadline'] != plan['created'] + VOTE_TIMEOUT
            or plan['activate_at'] != plan['deadline'] + ACTIVATION_LEAD):
        raise AgreementError('proposal extended or outside its round')
    members = plan['participants']
    if (not isinstance(members, dict) or not 1 <= len(members) <= MAX_MEMBERS
            or not isinstance(plan['leader'], str) or plan['leader'] not in members
            or not isinstance(plan['nonce'], str) or not TOKEN.fullmatch(plan['nonce'])
            or type(plan['limp']) is not bool or type(plan['moving']) is not bool):
        raise AgreementError('invalid proposal identity')
    for mac, boot in members.items():
        if (not isinstance(mac, str) or not MAC.fullmatch(mac)
                or (boot is not None and (not isinstance(boot, str) or not TOKEN.fullmatch(boot)))):
            raise AgreementError('invalid participant')
    if members[plan['leader']] is None:
        raise AgreementError('missing coordinator session')
    channels = plan['channels']
    if (not isinstance(channels, dict) or not channels
            or any(band not in CHANNELS or type(freq) is not int or
                   freq not in (CHANNELS[band] | (DFS_CHANNELS
                                if band == '5' and 'dfs' in plan else set()))
                   for band, freq in channels.items())):
        raise AgreementError('invalid target channels')
    if 'dfs' in plan:
        validate_dfs_contract(plan)


def compatible(channels, status):
    """Check target support on bands present in the supplied status."""
    if not valid_status(status):
        return False
    # A missing local band imposes no constraint on the shared plan.
    for band, freq in channels.items():
        if band in status['current']:
            supported = freq in status['allowed'][band]
            if band == '5' and freq in DFS_CHANNELS:
                allowed = status.get('dfs_allowed', [])
                supported = (dfs_version(status) == DFS_VERSION and
                             isinstance(allowed, list) and
                             any(type(f) is int and f == freq
                                 for f in allowed))
            if (not supported or
                    (not status['acs'] and freq != status['current'][band])):
                return False
    return True


def dfs_version(status):
    """Negotiate only explicitly advertised, implemented DFS versions."""
    versions = status.get('dfs_versions', []) if status else []
    if (isinstance(versions, list) and
            any(type(v) is int and v == DFS_VERSION for v in versions)):
        return DFS_VERSION
    return None


def valid_hold(hold):
    """Check telemetry shape without granting public telemetry authority."""
    keys = {'version', 'frequency', 'width', 'center', 'state', 'boot',
            'epoch', 'generation', 'live', 'observed_at', 'lease_until'}
    if not isinstance(hold, dict) or set(hold) != keys:
        return False
    for field in ('version', 'frequency', 'width', 'center', 'generation',
                  'observed_at', 'lease_until'):
        if type(hold[field]) is not int or not 0 <= hold[field] < 2**64:
            return False
    return (hold['version'] == DFS_VERSION and hold['width'] == 20 and
            hold['frequency'] in DFS_CHANNELS and
            hold['center'] == hold['frequency'] and
            hold['state'] in ('cac', 'held', 'revoked', 'fault') and
            type(hold['live']) is bool and
            all(isinstance(hold[k], str) and TOKEN.fullmatch(hold[k])
                for k in ('boot', 'epoch')))


def live_hold(status, now):
    """Return a bounded clearance from authenticated status, or no promise."""
    hold = status.get('dfs_hold') if status else None
    if (dfs_version(status) == DFS_VERSION and
            status.get('dfs_capable') is True and valid_hold(hold) and
            hold['boot'] == status.get('boot') and
            hold['state'] == 'held' and hold['live'] and
            hold['observed_at'] <= now < hold['lease_until'] and
            0 < hold['lease_until'] - hold['observed_at'] <=
            DFS_LEASE_SECONDS and
            hold['generation'] > 0):
        return hold
    return None


def dfs_contract(view, frequency, fallback, now):
    """Freeze every connected boot/receiver generation and one safe fallback."""
    holds = {mac: live_hold(status, now) for mac, status in view.items()}
    if (not holds or any(h is None or h['frequency'] != frequency
                         for h in holds.values())):
        raise AgreementError('DFS requires every connected live hold')
    contract = {'version': DFS_VERSION,
                'candidate': {'frequency': frequency, 'width': 20,
                              'center': frequency},
                'fallback': fallback,
                'holds': {mac: [h['epoch'], h['generation']]
                          for mac, h in holds.items()}}
    # Boot identity is already in participants; leases remain renewable.
    probe = {'channels': {'5': frequency}, 'dfs': contract,
             'participants': {mac: s['boot'] for mac, s in view.items()}}
    validate_dfs_contract(probe)
    if not dfs_ready(probe, view, now):
        raise AgreementError('DFS candidate or fallback is incompatible')
    return contract


def dfs_fallback(view):
    """Choose a common non-DFS fallback, preferring the current shared MHz."""
    common = set(CHANNELS['5'])
    for status in view.values():
        if not status or '5' not in status.get('current', {}):
            return None
        common &= {f for f in CHANNELS['5']
                   if compatible({'5': f}, status)}
    if not common:
        return None
    counts = {f: sum(s['current'].get('5') == f for s in view.values())
              for f in common}
    return {'5': min(common, key=lambda f: (-counts[f], f))}


def validate_dfs_contract(plan):
    """Reject partial, unknown-version or nonunanimous DFS contracts."""
    data = plan['dfs']
    frequency = plan['channels'].get('5')
    if (not isinstance(data, dict) or
            set(data) != {'version', 'candidate', 'fallback', 'holds'} or
            type(data['version']) is not int or
            data['version'] != DFS_VERSION or frequency not in DFS_CHANNELS or
            not isinstance(data['candidate'], dict) or
            any(type(v) is not int for v in data['candidate'].values()) or
            data['candidate'] != {'frequency': frequency, 'width': 20,
                                  'center': frequency} or
            not isinstance(data['fallback'], dict) or
            set(data['fallback']) != {'5'} or
            type(data['fallback']['5']) is not int or
            data['fallback']['5'] not in CHANNELS['5'] or
            not isinstance(data['holds'], dict) or
            set(data['holds']) != set(plan['participants']) or
            any(boot is None for boot in plan['participants'].values())):
        raise AgreementError('invalid DFS contract')
    for binding in data['holds'].values():
        if (not isinstance(binding, list) or len(binding) != 2 or
                not isinstance(binding[0], str) or
                not TOKEN.fullmatch(binding[0]) or
                type(binding[1]) is not int or not 0 < binding[1] < 2**64):
            raise AgreementError('invalid DFS receiver binding')


def dfs_ready(plan, view, now):
    """Recheck full connected membership and renewable authenticated leases."""
    if view is None or set(view) != set(plan['participants']):
        return False
    for mac, status in view.items():
        hold = live_hold(status, now) if isinstance(status, dict) else None
        if (not hold or not valid_status(status) or
                not status['ready'] or
                status.get('stable', True) is not True or
                status.get('discovery', False) is not False or
                status['boot'] != plan['participants'][mac] or
                [hold['epoch'], hold['generation']] !=
                plan['dfs']['holds'][mac] or
                any(hold[k] != v for k, v in
                    plan['dfs']['candidate'].items()) or
                '5' not in status.get('current', {}) or
                not compatible(plan['channels'], status) or
                not compatible(plan['dfs']['fallback'], status)):
            return False
    return True


def dfs_abort(plan, own, reason):
    """Bind a local cancellation to one plan, boot and receiver generation."""
    epoch, generation = plan['dfs']['holds'][own]
    return {'version': DFS_VERSION, 'plan': digest(plan),
            'boot': plan['participants'][own], 'epoch': epoch,
            'generation': generation, 'reason': reason}


def valid_dfs_abort(abort, plan, sender):
    """Accept only an authenticated participant's exact-generation abort."""
    if ('dfs' not in plan or sender not in plan['participants'] or
            not isinstance(abort, dict) or
            set(abort) != {'version', 'plan', 'boot', 'epoch', 'generation',
                           'reason'} or
            abort['reason'] not in ('radar', 'clearance_lost') or
            type(abort['version']) is not int or
            type(abort['generation']) is not int):
        return False
    return abort == dfs_abort(plan, sender, abort['reason'])


def require_dfs_activation(plan, view, now):
    """Require every participant and its current detector token at activation."""
    validate_plan(plan)
    if not dfs_ready(plan, view, now):
        raise AgreementError('DFS clearance changed before activation')
    return True


def can_vote(plan, own, view, now):
    """Require matching membership, coordinator, boot and a live deadline."""
    validate_plan(plan)
    validate_view(view)
    status = view.get(own)
    return bool(status and own in plan['participants']
                and plan['participants'][own] in (None, status['boot'])
                and plan['created'] <= now < plan['deadline']
                and set(plan['participants']) == set(view)
                and coordinator(view) == plan['leader'] and status['ready']
                and ('dfs' not in plan or dfs_ready(plan, view, now))
                and compatible(plan['channels'], status))


def valid_commit(commit, plan):
    """Authenticated coordinator proof, not independent peer signatures."""
    if not isinstance(commit, dict) or set(commit) != {'plan', 'approvals', 'issued_at'}:
        return False
    votes = commit['approvals']
    return (commit['plan'] == digest(plan) and type(commit['issued_at']) is int
            and plan['deadline'] <= commit['issued_at'] < plan['activate_at'] - COMMIT_CUTOFF
            and isinstance(votes, dict) and majority(plan) <= len(votes) <= len(plan['participants'])
            and plan['leader'] in votes
            and all(mac in plan['participants'] and isinstance(boot, str) and TOKEN.fullmatch(boot)
                    and plan['participants'][mac] in (None, boot) for mac, boot in votes.items()))


def live_destination(destination, status, now):
    """A fresh publisher currently operating an agreed plan can offer recovery.

    Freshness belongs to the authenticated *advertisement*, not the original
    activation time. A working plan can be days old. Callers supply only live
    reachable publishers, and must reconcile competing plans before adopting.
    """
    try:
        plan, commit = destination['plan'], destination['commit']
        validate_plan(plan)
        # Recovery never substitutes a cached certificate for live clearance.
        if 'dfs' in plan:
            return None
        if (valid_commit(commit, plan) and valid_status(status) and status['acs']
                and status.get('stable', True) is True
                and not status.get('discovery', False)
                and now >= plan['activate_at']
                and set(plan['channels']).intersection(status['current'])
                and all(status['current'][b] == f for b, f in plan['channels'].items()
                        if b in status['current'])):
            return destination
    except (ValueError, TypeError, KeyError):
        pass
    return None


def conflicting_destinations(destinations):
    """Disagreement on a shared band, not distinct certificates for one plan."""
    channels = {}
    for destination in destinations:
        for band, freq in destination['plan']['channels'].items():
            if band in channels and channels[band] != freq:
                return True
            channels[band] = freq
    return False


def network_destinations(destinations, members):
    """Distinguish a missed network commit from independently elected islands.

    A newer majority plan covering the whole currently reachable membership
    supersedes older plans. An island's newer timestamp alone is insufficient.
    Conflicting certificates from the same latest round also need a new round.
    """
    if not conflicting_destinations(destinations.values()):
        return destinations
    latest = max(d['plan']['round'] for d in destinations.values())
    newest = {mac: d for mac, d in destinations.items() if d['plan']['round'] == latest}
    covering = {mac: d for mac, d in newest.items() if set(members) <= set(d['plan']['participants'])}
    if covering and not conflicting_destinations(newest.values()):
        return covering
    return destinations


def advance(saved, own, now, view, records, proposal=None, local=None):
    """Return (state, outgoing, apply). Persist before publishing or applying.

    view=None means discovery failed, never an empty mesh. Existing promises
    survive that failure with their fixed deadline unchanged. A node votes for
    at most one plan per round, including across process restarts.
    """
    state = deepcopy(saved)
    current_round = now // ROUND_SECONDS
    # A backward clock step cannot reopen a completed or promised round.
    if current_round < state.get('round', -1):
        return state, {}, None
    if current_round > state.get('round', -1):
        state = {'round': current_round, 'phase': 'idle'}
    if state.get('phase') in ('attempted', 'expired', 'aborted'):
        if state.get('abort'):
            return state, {'abort': state['abort']}, None
        return state, {}, None
    local = local or (view or {}).get(own)
    plan = state.get('plan')
    if plan is None and view:
        candidate = proposal or records.get(coordinator(view), {}).get('proposal')
        if candidate is not None:
            try:
                if candidate['round'] == current_round and can_vote(candidate, own, view, now):
                    plan = deepcopy(candidate)
                    state.update(plan=plan, phase='prepared', local=snapshot(local),
                                 votes={own: local['boot']})
            except (AgreementError, KeyError, TypeError):
                pass
    if plan is None:
        return state, {}, None
    if 'dfs' in plan:
        for sender, record in records.items():
            abort = record.get('abort')
            if valid_dfs_abort(abort, plan, sender):
                state['phase'] = 'aborted'
                return state, {}, None
        if not dfs_ready(plan, view, now):
            state['phase'] = 'aborted'
            if local and local.get('boot') == plan['participants'][own]:
                state['abort'] = dfs_abort(plan, own, 'clearance_lost')
            return state, ({'abort': state['abort']}
                           if state.get('abort') else {}), None
    # A newly reachable group (over HaLow or any other interface) is part of
    # the same decision domain. Stop uncommitted island elections when
    # membership expands. Execute issued commitments, then reconcile in a
    # shared round.
    if view and set(view) - set(plan['participants']) and 'commit' not in state:
        state['phase'] = 'expired'
        return state, {}, None
    # Reboot or a channel change invalidates the local promise.
    landed_dfs = ('dfs' in plan and local and 'commit' in state and
                  valid_commit(state['commit'], plan) and
                  plan['activate_at'] <= now <= plan['activate_at'] + APPLY_GRACE and
                  all(local['current'].get(b) == f for b, f in plan['channels'].items()))
    if not local or (snapshot(local) != state['local'] and not landed_dfs):
        state['phase'] = 'expired'
        if ('dfs' in plan and local and
                local.get('boot') == plan['participants'][own]):
            state['phase'] = 'aborted'
            state['abort'] = dfs_abort(plan, own, 'clearance_lost')
            return state, {'abort': state['abort']}, None
        return state, {}, None
    plan_id = digest(plan)
    if own == plan['leader'] and 'commit' not in state:
        if now < plan['deadline']:
            for mac, boot in plan['participants'].items():
                vote = records.get(mac, {}).get('vote')
                if (isinstance(vote, dict) and set(vote) == {'plan', 'boot'}
                        and vote['plan'] == plan_id and isinstance(vote['boot'], str)
                        and TOKEN.fullmatch(vote['boot']) and boot in (None, vote['boot'])):
                    state['votes'][mac] = vote['boot']
        # Commit after voting closes, with at least five seconds to activate.
        elif now < plan['activate_at'] - COMMIT_CUTOFF:
            if len(state['votes']) >= majority(plan):
                state['commit'] = {'plan': plan_id, 'approvals': deepcopy(state['votes']), 'issued_at': now}
                state['phase'] = 'committed'
            else:
                state['phase'] = 'expired'
                return state, {}, None
    if 'commit' not in state and now < plan['activate_at'] - COMMIT_CUTOFF:
        commit = records.get(plan['leader'], {}).get('commit')
        if valid_commit(commit, plan) and commit['issued_at'] <= now:
            state['commit'] = deepcopy(commit)
            state['phase'] = 'committed'
    outgoing = {'proposal': plan} if own == plan['leader'] else {}
    outgoing['vote'] = {'plan': plan_id, 'boot': local['boot']}
    if 'commit' in state:
        outgoing['commit'] = state['commit']
    # Apply once, no later than five seconds after the agreed activation.
    if now >= plan['activate_at']:
        if 'commit' in state and now <= plan['activate_at'] + APPLY_GRACE:
            state['phase'] = 'attempted'
            return state, outgoing, plan
        state['phase'] = 'expired'
        return state, {}, None
    return state, outgoing, None
