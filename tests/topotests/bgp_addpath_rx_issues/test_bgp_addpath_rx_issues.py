#!/usr/bin/env python
# SPDX-License-Identifier: ISC

"""
test_bgp_addpath_rx_issues.py

Demonstrates three Add-Path RX problems in FRR (IPv4/IPv6 unicast only)
described in frr_addpath_rx_issues.md.

Topology
--------

                                +------------------+
                                |        r1        |
                                |     (AS 65001)   |
                                +------------------+
                                         |
                                   +-----------+
                                   |    s1     |
                                   +-----------+
                                         |
                                +------------------+
                                |        r2        |
                                |     (AS 65002)   |
                                +------------------+
                                         |
                                   +-----------+
                                   |    s2     |
                                   +-----------+
        /      /      /      /     |     \      \      \
+----------++----------++----------++----------++----------++----------++----------+
|    r3    ||    r4    ||    r5    ||    r6    ||    r7    ||    r8    ||    r9    |
|(AS 65003)||(AS 65004)||(AS 65005)||(AS 65006)||(AS 65007)||(AS 65008)||(AS 65009)|
+----------++----------++----------++----------++----------++----------++----------+

r3-r9 each originate 172.16.16.254/32, giving r2 seven distinct paths.
r2 is configured with addpath-tx-all-paths toward r1.
r1 is reconfigured across test phases to demonstrate each problem.

Problem 1 - Add-Path RX enabled by default (IPv4/IPv6 unicast)
--------------------------------------------------------------
PEER_FLAG_DISABLE_ADDPATH_RX is not set in peer_new(), so every new peer
silently advertises addpath-rx capability and accepts multiple paths.  An
operator who has not explicitly opted into Add-Path receive may be surprised
to find multiple paths per prefix installed in the RIB.

Problem 2 - No limit on the number of accepted Add-Path paths
-------------------------------------------------------------
When addpath-rx is enabled but addpath-rx-paths-limit is not configured,
addpath_paths_limit[afi][safi].send is 0.  Per the Paths-Limit draft, 0
means "no limit", so the peer sends all available paths and FRR accepts
them all.  A misbehaving or buggy peer can grow the RIB without bound.

Problem 3 - Paths-Limit is TX-only; no local enforcement
---------------------------------------------------------
addpath-rx-paths-limit N advertises N to the peer via the Paths-Limit
capability.  A cooperative peer honours this and caps its outgoing paths.
However, if the peer does not implement the draft, or misbehaves, it may
send more than N paths.  There is no local enforcement to drop the excess,
so FRR accepts every path the peer chooses to send.
"""

import os
import sys
import json
import pytest
import functools

CWD = os.path.dirname(os.path.realpath(__file__))
sys.path.append(os.path.join(CWD, "../"))

# pylint: disable=C0413
from lib import topotest
from lib.topogen import Topogen, get_topogen

pytestmark = [pytest.mark.bgpd]

NUM_SENDERS = 7


def build_topo(tgen):
    r1 = tgen.add_router("r1")
    r2 = tgen.add_router("r2")
    for i in range(3, 3 + NUM_SENDERS):
        tgen.add_router("r{}".format(i))

    s1 = tgen.add_switch("s1")
    s1.add_link(r1)
    s1.add_link(r2)

    s2 = tgen.add_switch("s2")
    s2.add_link(r2)
    for i in range(3, 3 + NUM_SENDERS):
        s2.add_link(tgen.gears["r{}".format(i)])


def setup_module(mod):
    tgen = Topogen(build_topo, mod.__name__)
    tgen.start_topology()

    for _, (rname, router) in enumerate(tgen.routers().items(), 1):
        router.load_frr_config(os.path.join(CWD, "{}/frr.conf".format(rname)))

    tgen.start_router()


def teardown_module(mod):
    tgen = get_topogen()
    tgen.stop_topology()


def _path_count(router, prefix="172.16.16.254/32"):
    """Return the number of BGP paths for *prefix* on *router*, or -1 on error."""
    try:
        out = json.loads(
            router.vtysh_cmd("show bgp ipv4 unicast {} json".format(prefix))
        )
        return len(out.get("paths", []))
    except Exception:
        return -1


def test_bgp_r2_has_all_paths():
    """
    Baseline: r2 must learn 172.16.16.254/32 from all seven senders before
    the per-problem tests are meaningful.
    """
    tgen = get_topogen()
    if tgen.routers_have_failure():
        pytest.skip(tgen.errors)

    r2 = tgen.gears["r2"]

    def _r2_converged():
        count = _path_count(r2)
        if count < NUM_SENDERS:
            return "r2 has {} paths, want {}".format(count, NUM_SENDERS)
        return None

    _, result = topotest.run_and_expect(
        functools.partial(_r2_converged), None, count=60, wait=1
    )
    assert result is None, result


def test_problem1_addpath_rx_enabled_by_default():
    """
    Problem 1: Add-Path RX is enabled by default.

    r1 carries no addpath configuration.  Because PEER_FLAG_DISABLE_ADDPATH_RX
    is not set in peer_new(), r1 silently advertises addpath-rx capability in
    its OPEN message.  r2 responds by sending all seven paths for
    172.16.16.254/32 via addpath-tx-all-paths.  The operator did not opt into
    receiving multiple paths, yet all seven paths appear in r1's RIB.
    """
    tgen = get_topogen()
    if tgen.routers_have_failure():
        pytest.skip(tgen.errors)

    r1 = tgen.gears["r1"]

    def _r1_receives_all_paths():
        count = _path_count(r1)
        if count != NUM_SENDERS:
            return "r1 has {} paths, want {}".format(count, NUM_SENDERS)
        return None

    _, result = topotest.run_and_expect(
        functools.partial(_r1_receives_all_paths), None, count=30, wait=1
    )
    assert result is None, (
        "Problem 1: r1 should receive all {} paths from r2 without any "
        "addpath-rx configuration — multiple paths are installed silently "
        "because addpath-rx is enabled by default".format(NUM_SENDERS)
    )


def test_problem2_no_rx_limit():
    """
    Problem 2: No limit on the number of accepted Add-Path paths.

    addpath-rx is explicitly enabled on r1 with no addpath-rx-paths-limit.
    addpath_paths_limit[afi][safi].send is therefore 0, which per the
    Paths-Limit draft means 'no limit'.  r2 sees no cap from r1 and sends
    all seven paths; FRR accepts all of them with no enforcement, leaving
    the router vulnerable to unbounded RIB growth if the peer misbehaves.
    """
    tgen = get_topogen()
    if tgen.routers_have_failure():
        pytest.skip(tgen.errors)

    r1 = tgen.gears["r1"]

    # Explicitly enable addpath-rx with no paths-limit configured.
    r1.vtysh_cmd(
        """
configure terminal
router bgp 65001
 address-family ipv4 unicast
  no neighbor 192.168.1.2 disable-addpath-rx
"""
    )

    # Force a session restart so the new OPEN reflects addpath-rx enabled.
    r1.vtysh_cmd("clear bgp 192.168.1.2")

    # r1 did not advertise a Paths-Limit to r2 (send == 0, no limit).
    def _no_paths_limit_advertised():
        out = json.loads(r1.vtysh_cmd("show bgp neighbor 192.168.1.2 json"))
        cap = (
            out.get("192.168.1.2", {})
            .get("neighborCapabilities", {})
            .get("pathsLimit", {})
            .get("ipv4Unicast", {})
        )
        advertised = cap.get("advertisedPathsLimit", 0)
        if advertised != 0:
            return "advertisedPathsLimit is {}, want 0 (no limit)".format(advertised)
        return None

    _, result = topotest.run_and_expect(
        functools.partial(_no_paths_limit_advertised), None, count=30, wait=1
    )
    assert result is None, (
        "Problem 2: r1 should not advertise a Paths-Limit when "
        "addpath-rx-paths-limit is not configured"
    )

    # r2 sends all paths because it sees no limit from r1; r1 accepts all of
    # them with no local enforcement to throttle the count.
    def _r1_accepts_all_paths():
        count = _path_count(r1)
        if count != NUM_SENDERS:
            return "r1 has {} paths, want {}".format(count, NUM_SENDERS)
        return None

    _, result = topotest.run_and_expect(
        functools.partial(_r1_accepts_all_paths), None, count=30, wait=1
    )
    assert result is None, (
        "Problem 2: r1 should accept all {} paths — there is no cap when "
        "addpath-rx-paths-limit is not configured".format(NUM_SENDERS)
    )


def test_problem3_tx_only_enforcement():
    """
    Problem 3: Paths-Limit enforcement is TX-only.

    r1 is configured with addpath-rx-paths-limit 4, which is advertised to r2
    via the Paths-Limit capability.  r2 is a cooperative FRR peer and respects
    the limit on its TX side, so r1 receives exactly 4 paths.

    The vulnerability is that this enforcement relies entirely on the peer's
    cooperation.  A peer that does not implement the Paths-Limit draft, or that
    has a bug, will send more than 4 paths regardless; there is no local
    enforcement on r1's receive side to drop the excess.
    """
    tgen = get_topogen()
    if tgen.routers_have_failure():
        pytest.skip(tgen.errors)

    r1 = tgen.gears["r1"]

    r1.vtysh_cmd(
        """
configure terminal
router bgp 65001
 address-family ipv4 unicast
  neighbor 192.168.1.2 addpath-rx-paths-limit 4
"""
    )

    # Force a full session restart so that Paths-Limit=4 is included in r1's
    # new OPEN message.  A dynamic CAPABILITY update alone is unreliable:
    # bgp_dynamic_capability_paths_limit() silently discards the update if r2
    # has not yet seen an Add-Path capability from r1 in a prior OPEN.
    r1.vtysh_cmd("clear bgp 192.168.1.2")

    # r1 must have advertised Paths-Limit=4 to r2 after the session resets.
    def _capability_advertised():
        out = json.loads(r1.vtysh_cmd("show bgp neighbor 192.168.1.2 json"))
        cap = (
            out.get("192.168.1.2", {})
            .get("neighborCapabilities", {})
            .get("pathsLimit", {})
            .get("ipv4Unicast", {})
        )
        if cap.get("advertisedPathsLimit") != 4:
            return "advertisedPathsLimit is {}, want 4".format(
                cap.get("advertisedPathsLimit")
            )
        return None

    _, result = topotest.run_and_expect(
        functools.partial(_capability_advertised), None, count=30, wait=1
    )
    assert result is None, result

    # r2 (cooperative) respects the advertised limit and sends only 4 paths.
    # A misbehaving peer would ignore the capability and send all NUM_SENDERS
    # paths; without local enforcement r1 would accept every one of them.
    def _r1_four_paths():
        count = _path_count(r1)
        if count != 4:
            return "r1 has {} paths, want 4".format(count)
        return None

    _, result = topotest.run_and_expect(
        functools.partial(_r1_four_paths), None, count=30, wait=1
    )
    assert result is None, (
        "Problem 3: the Paths-Limit is honoured only because r2 cooperates; "
        "a misbehaving peer would bypass it and r1 would accept all {} "
        "paths with no local guard".format(NUM_SENDERS)
    )


def test_memory_leak():
    "Run the memory leak test and report results."
    tgen = get_topogen()
    if not tgen.is_memleak_enabled():
        pytest.skip("Memory leak test/report is disabled")
    tgen.report_memory_leaks()


if __name__ == "__main__":
    args = ["-s"] + sys.argv[1:]
    sys.exit(pytest.main(args))
