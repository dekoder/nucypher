"""
This file is part of nucypher.

nucypher is free software: you can redistribute it and/or modify
it under the terms of the GNU Affero General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

nucypher is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU Affero General Public License for more details.

You should have received a copy of the GNU Affero General Public License
along with nucypher.  If not, see <https://www.gnu.org/licenses/>.
"""
import random
from unittest.mock import patch

import maya
import pytest
import time

import pytest_twisted
from flask import Response

from nucypher.utilities.sandbox.middleware import SluggishLargeFleetMiddleware
from umbral.keys import UmbralPublicKey
from unittest.mock import patch

from nucypher.characters.lawful import Ursula
from tests.utils.ursula import MOCK_KNOWN_URSULAS_CACHE
from umbral.keys import UmbralPublicKey
from nucypher.datastore.base import RecordField
from tests.mock.performance_mocks import (
    NotAPublicKey,
    NotARestApp,
    VerificationTracker,
    mock_cert_loading,
    mock_cert_storage,
    mock_message_verification,
    mock_metadata_validation,
    mock_pubkey_from_bytes,
    mock_secret_source,
    mock_signature_bytes,
    mock_stamp_call,
    mock_verify_node
)

"""
Node Discovery happens in phases.  The first step is for a network actor to learn about the mere existence of a Node.
This is a straightforward step which we currently do with our own logic, but which may someday be replaced by something
like libp2p, depending on the course of development of those sorts of tools.  The introduction of hamming distance
in particular is useful when wanting to learn about a small number (~500) of nodes among a much larger (25,000+) swarm.
This toolchain is not built for that scenario at this time, although it is not a stated nongoal. 

After this, our "Learning Loop" does four other things in sequence which are not part of the offering of node discovery tooling alone:

* Instantiation of an actual Node object (currently, an Ursula object) from node metadata.  TODO
* Validation of the node's metadata (non-interactive; shows that the Node's public material is indeed signed by the wallet holder of its Staker).
* Verification of the Node itself (interactive; shows that the REST server operating at the Node's interface matches the node's metadata).
* Verification of the Stake (reads the blockchain; shows that the Node is sponsored by a Staker with sufficient Stake to support a Policy).

These tests show that each phase of this process is done correctly, and in some cases, with attention to specific
performance bottlenecks.
"""


def test_alice_can_learn_about_a_whole_bunch_of_ursulas(highperf_mocked_alice):
    # During the fixture execution, Alice verified one node.
    # TODO: Consider changing this - #1449
    assert VerificationTracker.node_verifications == 1

    # A quick setup so that the bytes casting of Ursulas (on what in the real world will be the remote node)
    # doesn't take up all the time.
    _teacher = highperf_mocked_alice.current_teacher_node()
    _teacher_known_nodes_bytestring = _teacher.bytestring_of_known_nodes()
    _teacher.bytestring_of_known_nodes = lambda *args, **kwargs: _teacher_known_nodes_bytestring # TODO: Formalize this?  #1537

    with mock_cert_storage, mock_cert_loading, mock_verify_node, mock_message_verification, mock_metadata_validation:
        with mock_pubkey_from_bytes(), mock_stamp_call, mock_signature_bytes:
            started = time.time()
            highperf_mocked_alice.block_until_number_of_known_nodes_is(4000, learn_on_this_thread=True)
            ended = time.time()
            elapsed = ended - started

    assert elapsed < 6  # 6 seconds is still a little long to discover 4000 out of 5000 nodes, but before starting the optimization that went with this test, this operation took about 18 minutes on jMyles' laptop.
    assert VerificationTracker.node_verifications == 1  # We have only verified the first Ursula.
    assert sum(
        isinstance(u, Ursula) for u in highperf_mocked_alice.known_nodes) < 20  # We haven't instantiated many Ursulas.
    VerificationTracker.node_verifications = 0  # Cleanup

_POLICY_PRESERVER = []


@pytest.mark.parametrize('fleet_of_highperf_mocked_ursulas', [1000], indirect=True)
def test_alice_verifies_ursula_just_in_time(fleet_of_highperf_mocked_ursulas,
                                            highperf_mocked_alice,
                                            highperf_mocked_bob):
    # Patch the Datastore PolicyArrangement model with the highperf
    # NotAPublicKey
    not_public_key_record_field = RecordField(NotAPublicKey, encode=bytes,
                                              decode=NotAPublicKey.from_bytes)

    _umbral_pubkey_from_bytes = UmbralPublicKey.from_bytes

    def actual_random_key_instead(*args, **kwargs):
        _previous_bytes = args[0]
        serial = _previous_bytes[-5:]
        pubkey = NotAPublicKey(serial=serial)
        return pubkey

    def mock_set_policy(id_as_hex):
        return ""

    def mock_receive_treasure_map(treasure_map_id):
        return Response(bytes(), status=202)

    with NotARestApp.replace_route("receive_treasure_map", mock_receive_treasure_map):
        with NotARestApp.replace_route("set_policy", mock_set_policy):
            with patch('umbral.keys.UmbralPublicKey.__eq__', lambda *args, **kwargs: True):
                with patch('umbral.keys.UmbralPublicKey.from_bytes',
                           new=actual_random_key_instead):
                    with patch("nucypher.datastore.models.PolicyArrangement._alice_verifying_key",
                               new=not_public_key_record_field):
                        with mock_cert_loading, mock_metadata_validation, mock_message_verification:
                            with mock_secret_source():
                                policy = highperf_mocked_alice.grant(
                                    highperf_mocked_bob, b"any label", m=20, n=30,
                                    expiration=maya.when('next week'),
                                    publish_treasure_map=False)
    # TODO: Make some assertions about policy.
    total_verified = sum(node.verified_node for node in highperf_mocked_alice.known_nodes)
    assert total_verified == 30


@pytest_twisted.inlineCallbacks
@pytest.mark.parametrize('fleet_of_highperf_mocked_ursulas', [1000], indirect=True)
def test_mass_treasure_map_placement(fleet_of_highperf_mocked_ursulas,
                                     highperf_mocked_alice,
                                     highperf_mocked_bob):
    """
    Large-scale map placement with a middleware that simulates network latency.
    """

    highperf_mocked_alice.network_middleware = SluggishLargeFleetMiddleware()

    policy = _POLICY_PRESERVER[0]


    with patch('umbral.keys.UmbralPublicKey.__eq__', lambda *args, **kwargs: True), mock_metadata_validation:
        try:
            deferreds = policy.publish_treasure_map(network_middleware=highperf_mocked_alice.network_middleware)
        except Exception as e:
            # Retained for convenient breakpointing during test reuns.
            raise

        nodes_we_expect_to_have_the_map = highperf_mocked_bob.matching_nodes_among(highperf_mocked_alice.known_nodes)

        def map_is_probably_about_ten_percent_published(*args, **kwargs):
            nodes_that_have_the_map_when_we_unblock = []

            for ursula in fleet_of_highperf_mocked_ursulas:
                if policy.treasure_map in list(ursula.treasure_maps.values()):
                    nodes_that_have_the_map_when_we_unblock.append(ursula)

            approximate_expected_distribution = int(len(nodes_we_expect_to_have_the_map) / 10)
            assert len(nodes_that_have_the_map_when_we_unblock) == pytest.approx(approximate_expected_distribution, 2)

        deferreds.addCallback(map_is_probably_about_ten_percent_published)

        # Fun fact: if you outdent this next line, you'll be unable to validate nodes - pytest
        # fires the callback of the DeferredList in the context of the yield (as it ought to).
        yield deferreds

        # Now we're back over here (which will be in the threadpool in the background in the real world, but in the main thread
        # for the remainder of this test), distributing the test to the rest of the eligible nodes.
        resumed_publication = maya.now()

        nodes_that_actually_have_the_map_eventually = []
        nodes_we_expect_to_have_the_map_but_which_do_not = [u for u in fleet_of_highperf_mocked_ursulas if u in nodes_we_expect_to_have_the_map]

        while nodes_we_expect_to_have_the_map_but_which_do_not:
            for ursula in nodes_we_expect_to_have_the_map_but_which_do_not:
                if policy.treasure_map in list(ursula.treasure_maps.values()):
                    nodes_that_actually_have_the_map_eventually.append(ursula)
                    nodes_we_expect_to_have_the_map_but_which_do_not.remove(ursula)
            time_spent_publishing = maya.now() - resumed_publication
            if time_spent_publishing.seconds > 30:
                pytest.fail("Treasure Map wasn't published to the rest of the eligible fleet.")
            time.sleep(.01)

        # For clarity.
        assert nodes_that_actually_have_the_map_eventually == nodes_we_expect_to_have_the_map