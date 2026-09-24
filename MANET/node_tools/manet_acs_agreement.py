"""Bounded majority ACS agreement; authenticated I/O lives in the runner.

A coordinator proposes one immutable plan per round. Votes bind the complete
plan, membership and activation time. This is partition-local agreement, not
atomic delivery or a global consensus protocol across disconnected meshes.
"""
from copy import deepcopy
import hashlib
import json
import re
from manet_rendezvous import RECOVERY_SECONDS

ROUND_SECONDS = 180
PROPOSE_FROM = 45  # Allow the :25 readiness requests to replicate first.
PROPOSE_UNTIL = 60
VOTE_TIMEOUT = 60
ACTIVATION_LEAD = 30
COMMIT_CUTOFF = 5
APPLY_GRACE = 5
MAX_MEMBERS = 64
CHANNELS = {'2.4': {2437, 2462}, '5': {5200, 5220, 5240, 5745, 5765, 5785, 5805, 5825}}
MAC = re.compile(r'(?:[0-9a-f]{2}:){5}[0-9a-f]{2}')
TOKEN = re.compile(r'[0-9a-f]{32}')


class AgreementError(ValueError):
    pass


def digest(plan):
    return hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(',', ':'),
                                    allow_nan=False).encode()).hexdigest()


def snapshot(status):
    return {'boot': status['boot'], 'current': status['current']}


def valid_status(status):
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
    if (not isinstance(view, dict) or not 1 <= len(view) <= MAX_MEMBERS
            or any(not isinstance(mac, str) or not MAC.fullmatch(mac)
                   or (status is not None and not valid_status(status)) for mac, status in view.items())):
        raise AgreementError('invalid participant view')


def coordinator(view):
    candidates = [mac for mac, s in view.items() if s and s['acs'] and s['ready'] and s['current']]
    return min(candidates) if candidates else None


def majority(plan):
    return len(plan['participants']) // 2 + 1


def make_plan(view, channels, limp, now, nonce):
    validate_view(view)
    leader = coordinator(view)
    if leader is None:
        raise AgreementError('no ready ACS coordinator')
    plan = {'round': now // ROUND_SECONDS, 'created': now,
            'deadline': now + VOTE_TIMEOUT, 'activate_at': now + VOTE_TIMEOUT + ACTIVATION_LEAD,
            'nonce': nonce, 'leader': leader, 'channels': channels, 'limp': limp,
            'moving': any(s and any(s['current'].get(b, f) != f for b, f in channels.items())
                          for s in view.values()),
            'participants': {mac: s['boot'] if s else None for mac, s in sorted(view.items())}}
    validate_plan(plan)
    return plan


def validate_plan(plan):
    keys = {'round', 'created', 'deadline', 'activate_at', 'nonce', 'leader', 'channels', 'limp', 'moving', 'participants'}
    if not isinstance(plan, dict) or set(plan) != keys:
        raise AgreementError('invalid proposal fields')
    if any(type(plan[k]) is not int or plan[k] < 0 for k in ('round', 'created', 'deadline', 'activate_at')):
        raise AgreementError('invalid proposal times')
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
            or any(band not in CHANNELS or type(freq) is not int or freq not in CHANNELS[band]
                   for band, freq in channels.items())):
        raise AgreementError('invalid target channels')


def compatible(channels, status):
    if not valid_status(status):
        return False
    for band, freq in channels.items():
        if band in status['current']:
            if freq not in status['allowed'][band] or (not status['acs'] and freq != status['current'][band]):
                return False
    return True


def can_vote(plan, own, view, now):
    validate_plan(plan)
    validate_view(view)
    status = view.get(own)
    return bool(status and own in plan['participants']
                and plan['participants'][own] in (None, status['boot'])
                and plan['created'] <= now < plan['deadline']
                and set(plan['participants']) == set(view)
                and coordinator(view) == plan['leader'] and status['ready']
                and compatible(plan['channels'], status))


def valid_commit(commit, plan):
    """Authenticated coordinator attestation; not independent peer signatures."""
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
    """Return (state, outgoing, apply). Persist BEFORE publishing or applying.

    view=None means discovery failed, never an empty mesh. Existing promises
    survive that failure; their fixed deadline is unchanged. A node votes for
    at most one plan per round, including across process restarts.
    """
    state = deepcopy(saved)
    current_round = now // ROUND_SECONDS
    if current_round < state.get('round', -1):
        return state, {}, None
    if current_round > state.get('round', -1):
        state = {'round': current_round, 'phase': 'idle'}
    if state.get('phase') in ('attempted', 'expired'):
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
    # A newly reachable group (over HaLow or any other interface) is part of
    # the same decision domain. Don't finish an uncommitted island election
    # after learning that membership has expanded. Already-issued commitments
    # still execute; reconciliation follows in a later shared round.
    if view and set(view) - set(plan['participants']) and 'commit' not in state:
        state['phase'] = 'expired'
        return state, {}, None
    if not local or snapshot(local) != state['local']:
        state['phase'] = 'expired'
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
    if now >= plan['activate_at']:
        if 'commit' in state and now <= plan['activate_at'] + APPLY_GRACE:
            state['phase'] = 'attempted'
            return state, outgoing, plan
        state['phase'] = 'expired'
        return state, {}, None
    return state, outgoing, None
