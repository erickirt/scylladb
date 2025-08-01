#
# Copyright (C) 2024-present ScyllaDB
#
# SPDX-License-Identifier: LicenseRef-ScyllaDB-Source-Available-1.0
#

# Multi-node tests for Alternator.
#
# Please note that most tests for Alternator are single-node tests and can
# be found in the test/alternator directory. Most functional testing of the
# many different syntax features that Alternator provides don't need more
# than a single node to be tested, and should be able to run also on DynamoDB
# - not just on Alternator, which the test/alternator framework allows to do.
# So only the minority of tests that do need a bigger cluster should be here.

import pytest
import asyncio
import logging
import time
import boto3
import botocore
from botocore.exceptions import ClientError
import requests
import json
from cassandra.auth import PlainTextAuthProvider
import threading
import random

from test.pylib.manager_client import ManagerClient
from test.pylib.util import wait_for
from test.pylib.tablets import get_all_tablet_replicas
from test.cluster.conftest import skip_mode

logger = logging.getLogger(__name__)

# Convenience function to open a connection to Alternator usable by the
# AWS SDK.
alternator_config = {
    'alternator_port': 8000,
    'alternator_write_isolation': 'only_rmw_uses_lwt',
    'alternator_ttl_period_in_seconds': '0.5',
}
def get_alternator(ip, user='alternator', passwd='secret_pass'):
    url = f"http://{ip}:{alternator_config['alternator_port']}"
    return boto3.resource('dynamodb', endpoint_url=url,
        region_name='us-east-1',
        aws_access_key_id=user,
        aws_secret_access_key=passwd,
        config=botocore.client.Config(
            retries={"max_attempts": 0},
            read_timeout=300)
    )

# Alternator convenience function for fetching the entire result set of a
# query into an array of items.
def full_query(table, ConsistentRead=True, **kwargs):
    response = table.query(ConsistentRead=ConsistentRead, **kwargs)
    items = response['Items']
    while 'LastEvaluatedKey' in response:
        response = table.query(ExclusiveStartKey=response['LastEvaluatedKey'],
            ConsistentRead=ConsistentRead, **kwargs)
        items.extend(response['Items'])
    return items

# FIXME: boto3 is NOT async. So all tests that use it are not really async.
# We could use the aioboto3 library to write a really asynchronous test, or
# implement an async wrapper to the boto3 functions ourselves (e.g., run them
# in a separate thread) ourselves.


test_table_prefix = 'alternator_Test_'
def unique_table_name():
    current_ms = int(round(time.time() * 1000))
    # If unique_table_name() is called twice in the same millisecond...
    if unique_table_name.last_ms >= current_ms:
        current_ms = unique_table_name.last_ms + 1
    unique_table_name.last_ms = current_ms
    return test_table_prefix + str(current_ms)
unique_table_name.last_ms = 0


async def test_alternator_ttl_scheduling_group(manager: ManagerClient):
    """A reproducer for issue #18719: The expiration scans and deletions
       initiated by the Alternator TTL feature are supposed to run entirely in
       the "streaming" scheduling group. But because of a bug in inheritance
       of scheduling groups through RPC, some of the work ended up being done
       on the "statement" scheduling group.
       This test verifies that Alternator TTL work is done on the right
       scheduling group.
       This test assumes that the cluster is not concurrently busy with
       running any other workload - so we won't see any work appearing
       in the wrong scheduling group. We can assume this because we don't
       run multiple tests in parallel on the same cluster.
    """
    servers = await manager.servers_add(3, config=alternator_config)
    alternator = get_alternator(servers[0].ip_addr)
    table = alternator.create_table(TableName=unique_table_name(),
        BillingMode='PAY_PER_REQUEST',
        KeySchema=[
            {'AttributeName': 'p', 'KeyType': 'HASH' },
        ],
        AttributeDefinitions=[
            {'AttributeName': 'p', 'AttributeType': 'N' },
        ])
    # Enable expiration (TTL) on attribute "expiration"
    table.meta.client.update_time_to_live(TableName=table.name, TimeToLiveSpecification={'AttributeName': 'expiration', 'Enabled': True})

    # Insert N rows, setting them all to expire 3 seconds from now.
    N = 100
    expiration = int(time.time())+3
    with table.batch_writer() as batch:
        for p in range(N):
            batch.put_item(Item={'p': p, 'expiration': expiration})


    # Unfortunately, Alternator has no way of doing the writes above with
    # CL=ALL, only CL=QUORUM. So at this point we're not sure all the writes
    # above have completed. We want to wait until they are over, so that we
    # won't measure any of those writes in the statement scheduling group.
    # Let's do it by checking the metrics of background writes and wait for
    # them to drop to zero.
    ips = [server.ip_addr for server in await manager.running_servers()]
    timeout = time.time() + 60
    while True:
        if time.time() > timeout:
            pytest.fail("timed out waiting for background writes to complete")
        bg_writes = 0
        for ip in ips:
            metrics = await manager.metrics.query(ip)
            bg_writes += metrics.get('scylla_storage_proxy_coordinator_background_writes')
        if bg_writes == 0:
            break # done waiting for the background writes to finish
        await asyncio.sleep(0.1)

    # Get the current amount of work (in CPU ms) done across all nodes and
    # shards in different scheduling groups. We expect this to increase
    # considerably for the streaming group while expiration scanning is
    # proceeding, but not increase at all for the statement group because
    # there are no requests being executed.
    async def get_cpu_metrics():
        ms_streaming = 0
        ms_statement = 0
        for ip in ips:
            metrics = await manager.metrics.query(ip)
            ms_streaming += metrics.get('scylla_scheduler_runtime_ms', {'group': 'streaming'})
            # in enterprise, default execution is in sl:default, not statement
            ms_statement += metrics.get('scylla_scheduler_runtime_ms', {'group': 'sl:default'})
        return (ms_streaming, ms_statement)

    ms_streaming_before, ms_statement_before = await get_cpu_metrics()

    # Wait until all rows expire, and get the CPU metrics again. All items
    # were set to expire in 3 seconds, and the expiration thread is set up
    # in alternator_config to scan the whole table in 0.5 seconds, and the
    # whole table is just 100 rows, so we expect all the data to be gone in
    # 4 seconds. Let's wait 5 seconds just in case. Even if not all the data
    # will have been deleted by then, we do expect some deletions to have
    # happened, and certainly several scans, all taking CPU which we expect
    # to be in the right scheduling group.
    await asyncio.sleep(5)
    ms_streaming_after, ms_statement_after = await get_cpu_metrics()

    # As a sanity check, verify some of the data really expired, so there
    # was some TTL work actually done. We actually expect all of the data
    # to have been expired by now, but in some extremely slow builds and
    # test machines, this may not be the case.
    assert N > table.scan(ConsistentRead=True, Select='COUNT')['Count']

    # Between the calls to get_cpu_metrics() above, several expiration scans
    # took place (we configured scans to happen every 0.5 seconds), and also
    # a lot of deletes when the expiration time was reached. We expect all
    # that work to have happened in the streaming group, not statement group,
    # so "ratio" calculate below should be tiny, even exactly zero. Before
    # issue #18719 was fixed, it was not tiny at all - 0.58.
    # Just in case there are other unknown things happening, let's assert it
    # is <0.1 instead of zero.
    ms_streaming = ms_streaming_after - ms_streaming_before
    ms_statement = ms_statement_after - ms_statement_before
    ratio = ms_statement / ms_streaming
    assert ratio < 0.1

    table.delete()

@pytest.mark.asyncio
async def test_localnodes_broadcast_rpc_address(manager: ManagerClient):
    """Test that if the "broadcast_rpc_address" of a node is set, the
       "/localnodes" request returns not the node's internal IP address,
       but rather the one set in broadcast_rpc_address as passed between
       nodes via gossip. The case where this parameter is not configured is
       tested separately, in test/alternator/test_scylla.py.
       Reproduces issue #18711.
    """
    # Run two Scylla nodes telling both their broadcast_rpc_address is 127.0.0.0
    # (this is silly, but servers_add() doesn't let us use a different config
    # per server). We need to run two nodes to check that the node to which
    # we send the /localnodes request knows not only its own modified
    # address, but also the other node's (which it learnt by gossip).
    # This address isn't used for any communication, but it will be
    # produced by "/localnodes" and this is what we want to check
    # The address "127.0.0.0" is a silly non-existing address which connecting
    # to fails immediately (this is useful in the test shutdown - we don't want
    # it to hang trying to reach this node, as happened in issue #22744).
    config = alternator_config | {
        'broadcast_rpc_address': '127.0.0.0'
    }
    servers = await manager.servers_add(2, config=config)
    for server in servers:
        # We expect /localnodes to return ["127.0.0.0", "127.0.0.0"]
        # (since we configured both nodes with the same broadcast_rpc_address).
        # We need the retry loop below because the second node might take a
        # bit of time to bootstrap after coming up, and only then will it
        # appear on /localnodes (see #19694).
        url = f"http://{server.ip_addr}:{config['alternator_port']}/localnodes"
        timeout = time.time() + 60
        while True:
            assert time.time() < timeout
            response = requests.get(url, verify=False)
            j = json.loads(response.content.decode('utf-8'))
            if j == ['127.0.0.0', '127.0.0.0']:
                break # done
            await asyncio.sleep(0.1)

@pytest.mark.asyncio
async def test_localnodes_drained_node(manager: ManagerClient):
    """Test that if in a cluster one node is brought down with "nodetool drain"
       a "/localnodes" request should NOT return that node. This test does
       NOT reproduce issue #19694 - a DRAINED node is not considered is_alive()
       and even before the fix of that issue, "/localnodes" didn't return it.
    """
    # Start a cluster with two nodes and verify that at this point,
    # "/localnodes" on the first node returns both nodes.
    # We the retry loop below because the second node might take a
    # bit of time to bootstrap after coming up, and only then will it
    # appear on /localnodes (see #19694).
    servers = await manager.servers_add(2, config=alternator_config)
    localnodes_request = f"http://{servers[0].ip_addr}:{alternator_config['alternator_port']}/localnodes"
    async def check_localnodes_two():
        response = requests.get(localnodes_request)
        j = json.loads(response.content.decode('utf-8'))
        if set(j) == {servers[0].ip_addr, servers[1].ip_addr}:
            return True
        elif set(j).issubset({servers[0].ip_addr, servers[1].ip_addr}):
            return None # try again
        else:
            return False
    assert await wait_for(check_localnodes_two, time.time() + 60)
    # Now "nodetool" drain on the second node, leaving the second node
    # in DRAINED state.
    await manager.api.client.post("/storage_service/drain", host=servers[1].ip_addr)
    # After that, "/localnodes" should no longer return the second node.
    # It might take a short while until the first node learns what happened
    # to node 1, so we may need to retry for a while
    async def check_localnodes_one():
        response = requests.get(localnodes_request)
        j = json.loads(response.content.decode('utf-8'))
        if set(j) == {servers[0].ip_addr, servers[1].ip_addr}:
            return None # try again
        elif set(j) == {servers[0].ip_addr}:
            return True
        else:
            return False
    assert await wait_for(check_localnodes_one, time.time() + 60)


@pytest.mark.asyncio
async def test_localnodes_down_normal_node(manager: ManagerClient):
    """Test that if in a cluster one node reaches "normal" state and then
       brought down (so is now in "DN" state), a "/localnodes" request
       should NOT return that node. Reproduces issue #21538.
    """
    # Start a cluster with two nodes and verify that at this point,
    # "/localnodes" on the first node returns both nodes.
    # We the retry loop below because the second node might take a
    # bit of time to bootstrap after coming up, and only then will it
    # appear on /localnodes (see #19694).
    servers = await manager.servers_add(2, config=alternator_config)
    localnodes_request = f"http://{servers[0].ip_addr}:{alternator_config['alternator_port']}/localnodes"
    async def check_localnodes_two():
        response = requests.get(localnodes_request)
        j = json.loads(response.content.decode('utf-8'))
        if set(j) == {servers[0].ip_addr, servers[1].ip_addr}:
            return True
        elif set(j).issubset({servers[0].ip_addr, servers[1].ip_addr}):
            return None # try again
        else:
            return False
    assert await wait_for(check_localnodes_two, time.time() + 60)
    # Now stop the second node abruptly with server_stop(). The server will
    # be down, the gossiper on the first node will soon realize it is down,
    # but still consider it in a "normal" state - "DN" (down and normal).
    # We then want to check that "/localnodes" handles this state correctly.
    await manager.server_stop(servers[1].server_id)
    # After that, "/localnodes" should no longer return the second node.
    # It might take a short while until the first node learns what happened
    # to the second, so we may need to retry for a while.
    async def check_localnodes_one():
        response = requests.get(localnodes_request)
        j = json.loads(response.content.decode('utf-8'))
        if set(j) == {servers[0].ip_addr, servers[1].ip_addr}:
            return None # try again
        elif set(j) == {servers[0].ip_addr}:
            return True
        else:
            return False
    assert await wait_for(check_localnodes_one, time.time() + 60)


@pytest.mark.asyncio
@skip_mode('release', 'error injections are not supported in release mode')
async def test_localnodes_joining_nodes(manager: ManagerClient):
    """Test that if a cluster is being enlarged and a node is coming up but
       not yet responsive, a "/localnodes" request should NOT return that node.
       Reproduces issue #19694.
    """
    # Start a cluster with one node, and then bring up a second node,
    # pausing its bootstrap (with an injection) in JOINING state.
    # We need to start the second node in the background, because server_add()
    # will wait for the bootstrap to complete - which we don't want to do.
    server = await manager.server_add(config=alternator_config)
    task = asyncio.create_task(manager.server_add(config=alternator_config | {'error_injections_at_startup': ['delay_bootstrap_120s']}))
    # Sleep until the first node knows of the second one as a "live node"
    # (we check this with the REST API's /gossiper/endpoint/live.
    async def check_two_live_nodes():
        j = await manager.api.client.get_json("/gossiper/endpoint/live", host=server.ip_addr)
        if len(j) == 1:
            return None # try again
        elif len(j) == 2:
            return True
        else:
            return False
    assert await wait_for(check_two_live_nodes, time.time() + 60)

    # At this point the second node is live, but hasn't finished bootstrapping
    # (we delayed that with the injection). So the "/localnodes" should still
    # return just one node - not both. Reproduces #19694 (two nodes used to
    # be returned)
    localnodes_request = f"http://{server.ip_addr}:{alternator_config['alternator_port']}/localnodes"
    response = requests.get(localnodes_request)
    j = json.loads(response.content.decode('utf-8'))
    assert len(j) == 1

    # We don't want to wait for the second server to finish its long
    # injection-caused bootstrap delay, so we won't check here that when the
    # second server finally comes up, both nodes will finally be visible in
    # /localnodes. This case is checked in other tests, where bootstrap
    # finishes normally, so we don't need to check this case again here.
    # But we can't just finish here with "task" unwaited or we'll get a
    # warning about an unwaited coroutine, and the ScyllaClusterManager's
    # tasks_history will wait for it anyway. For the same reason we can't
    # task.cancel() (this will cause ScyllaClusterManager's tasks_history
    # to report the ScyllaClusterManager got BROKEN and fail the next test).
    # Sadly even abruptly killing the servers (with manager.server_stop())
    # (with the intention to then "await task" quickly) doesn't work,
    # probably because of a bug in the library. So we "await task"
    # anyway, and this test takes 2 minutes :-(
    #for server in await manager.all_servers():
    #    await manager.server_stop(server.server_id)
    await task

@pytest.mark.asyncio
async def test_localnodes_multi_dc_multi_rack(manager: ManagerClient):
    """A test for /localnodes on a more general setup, with multiple DCs and
       multiple racks - an 8-node setup with two DCs, two racks in each, and
       two nodes in each rack.
       Test both the default of returning the nodes on DC of the server being
       connected - and the "dc" and "rack" options for explicitly choosing a
       specific dc and/or rack.
    """
    # Start 8 nodes on two different dcs (called "dc1" and "dc2") and two
    # different racks ("rack1" and "rack2"), two nodes in each rack.
    config = alternator_config | {
        'endpoint_snitch': 'GossipingPropertyFileSnitch'
    }
    servers = {}
    for dc in ['dc1', 'dc2']:
        for rack in ['rack1', 'rack2']:
            servers[dc,rack] = await manager.servers_add(2, config=config, property_file={
                'dc': dc, 'rack': rack})

    def localnodes_request(server):
        return f"http://{server.ip_addr}:{alternator_config['alternator_port']}/localnodes"

    # Before we test various variations of the /localnodes request, let's wait
    # until all nodes are visible to each other in /localnodes requests. This
    # can take time, while nodes finish bootstrapping and gossip to each other
    # (see #19694). After this one-time wait_for, the following checks will be
    # able to check things immediately - without retries.
    for dc in ['dc1', 'dc2']:
        for rack in ['rack1', 'rack2']:
            for server in servers[dc, rack]:
                async def check_localnodes_eight():
                    for option_dc in ['dc1', 'dc2']:
                        response = requests.get(localnodes_request(server), {'dc': option_dc})
                        if len(json.loads(response.content.decode('utf-8'))) < 4:
                            return None # try again
                    return True
                assert await wait_for(check_localnodes_eight, time.time() + 60)

    # Check that the option-less "/localnodes" returns for each of dc1's nodes
    # the four dc1 servers, and for each of dc2's nodes, the four dc2 servers:
    for dc in ['dc1', 'dc2']:
        dc_servers = servers[dc, 'rack1'] + servers[dc, 'rack2']
        expected_ips = [server.ip_addr for server in dc_servers]
        for server in dc_servers:
            response = requests.get(localnodes_request(server))
            assert sorted(json.loads(response.content.decode('utf-8'))) == sorted(expected_ips)

    # Check that the "dc" option works - it should return the nodes for the
    # specified DC, regardless of which node on which DC the request is sent
    # to (we test all combinations of one of 8 target nodes and 2 option dcs).
    all_servers = sum(servers.values(), [])
    for option_dc in ['dc1', 'dc2']:
        expected_servers = servers[option_dc, 'rack1'] + servers[option_dc, 'rack2']
        expected_ips = [server.ip_addr for server in expected_servers]
        for server in all_servers:
            response = requests.get(localnodes_request(server), {'dc': option_dc})
            assert sorted(json.loads(response.content.decode('utf-8'))) == sorted(expected_ips)

    # Check that the "rack" option works (without "dc") - it returns for each of dc1's
    # nodes the same two servers from the specified rack in dc1, and for each of dc2's
    # nodes, the same two dc2 servers in the specified rack:
    for dc in ['dc1', 'dc2']:
        dc_servers = servers[dc, 'rack1'] + servers[dc, 'rack2']
        for option_rack in ['rack1', 'rack2']:
            expected_ips = [server.ip_addr for server in servers[dc, option_rack]]
            for server in dc_servers:
                response = requests.get(localnodes_request(server), {'rack': option_rack})
                assert sorted(json.loads(response.content.decode('utf-8'))) == sorted(expected_ips)

    # Check that a combination of the "rack" and "dc" option works - it always returns
    # the same two nodes belonging to the given rack and dc, no matter which of the 8
    # servers the request is sent to.
    for option_dc in ['dc1', 'dc2']:
        for option_rack in ['rack1', 'rack2']:
            expected_ips = [server.ip_addr for server in servers[option_dc, option_rack]]
            for server in all_servers:
                response = requests.get(localnodes_request(server), {'dc': option_dc, 'rack': option_rack})
                assert sorted(json.loads(response.content.decode('utf-8'))) == sorted(expected_ips)


# We have in test/alternator/test_cql_rbac.py many functional tests for
# CQL-based Role Based Access Control (RBAC) and all those tests use the
# same one-node cluster with authentication and authorization enabled.
# Here in this file we have the opportunity to create clusters with different
# configurations, so we can check how these configuration settings affect RBAC.
@pytest.mark.asyncio
async def test_alternator_enforce_authorization_false(manager: ManagerClient):
    """A basic test for how Alternator authentication and authorization
       work when alternator_enfore_authorization is *false* (and CQL's
       authenticator/authorizer options are also unset):
       1. Username and signature is not checked - a request with a bad
          username is accepted.
       2. Any user (or even non-existent user) has permissions to do any
          operation.
    """
    servers = await manager.servers_add(1, config=alternator_config)
    # Requests from a non-existent user with garbage password work,
    # and can perform privildged operations like CreateTable, etc.
    alternator = get_alternator(servers[0].ip_addr, 'nonexistent_user', 'garbage')
    table = alternator.create_table(TableName=unique_table_name(),
        BillingMode='PAY_PER_REQUEST',
        KeySchema=[ {'AttributeName': 'p', 'KeyType': 'HASH' } ],
        AttributeDefinitions=[ {'AttributeName': 'p', 'AttributeType': 'N' } ])
    table.put_item(Item={'p': 42})
    table.get_item(Key={'p': 42})
    table.delete()

async def test_alternator_enforce_authorization_false2(manager: ManagerClient):
    """A variant of the above test for alternator_enforce_authorization=false
       Here we check what happens when CQL's authenticator/authorizer are
       enabled (in the previous test they were disabled).
       This combination of configuration options isn't very useful (setting
       authenticator/authorizer is only needed for RBAC, so why set it when
       RBAC is not supposed to be enabled?), but it also shouldn't break
       alternator_enfore_authorization=false - requests should be allowed
       regardless of how they are signed.
       Reproduces issue #20619.
    """
    config = alternator_config | {
        'alternator_enforce_authorization': False,
        'authenticator': 'PasswordAuthenticator',
        'authorizer': 'CassandraAuthorizer'
    }
    servers = await manager.servers_add(1, config=config,
        driver_connect_opts={'auth_provider': PlainTextAuthProvider(username='cassandra', password='cassandra')})
    # Requests from a non-existent user with garbage password work,
    # and can perform privildged operations like CreateTable, etc.
    # It is important to exercise CreateTable as well, because it has
    # special auto-grant code that we want to check as well.
    alternator = get_alternator(servers[0].ip_addr, 'nonexistent_user', 'garbage')
    table = alternator.create_table(TableName=unique_table_name(),
        BillingMode='PAY_PER_REQUEST',
        KeySchema=[ {'AttributeName': 'p', 'KeyType': 'HASH' } ],
        AttributeDefinitions=[ {'AttributeName': 'p', 'AttributeType': 'N' } ])
    table.put_item(Item={'p': 42})
    table.get_item(Key={'p': 42})
    table.delete()

def get_secret_key(cql, user):
    """The secret key used for a user in Alternator is its role's salted_hash.
       This function retrieves it from the system table.
    """
    # Newer Scylla places the "roles" table in the "system" keyspace, but
    # older versions used "system_auth_v2" or "system_auth"
    for ks in ['system', 'system_auth_v2', 'system_auth']:
        try:
            e = list(cql.execute(f"SELECT salted_hash FROM {ks}.roles WHERE role = '{user}'"))
            if e != [] and e[0].salted_hash is not None:
                return e[0].salted_hash
        except:
            pass
    pytest.fail(f"Couldn't get secret key for user {user}")

@pytest.mark.skip("flaky, needs to be fixed, see https://github.com/scylladb/scylladb/pull/20135")
@pytest.mark.asyncio
async def test_alternator_enforce_authorization_true(manager: ManagerClient):
    """A basic test for how Alternator authentication and authorization
       work when authentication and authorization is enabled in CQL, and
       additionally alternator_enfore_authorization is *true*:
       1. The username and signature is verified (a request with a bad
          username or password is rejected)
       2. A new user works, and can do things that don't need permissions
          (such as ListTables) but can't perform operations that do need
          permissions (e.g., CreateTable).
    """
    config = alternator_config | {
        'alternator_enforce_authorization': True,
        'authenticator': 'PasswordAuthenticator',
        'authorizer': 'CassandraAuthorizer'
    }
    servers = await manager.servers_add(1, config=config,
        driver_connect_opts={'auth_provider': PlainTextAuthProvider(username='cassandra', password='cassandra')})
    cql = manager.get_cql()
    # Any requests from a non-existent user with garbage password is
    # rejected - even requests that don't need special permissions
    alternator = get_alternator(servers[0].ip_addr, 'nonexistent_user', 'garbage')
    with pytest.raises(ClientError, match='UnrecognizedClientException'):
        alternator.meta.client.list_tables()
    # We know that Scylla is set up with a "cassandra" user. If we retrieve
    # its correct secret key, the ListTables will work.
    alternator = get_alternator(servers[0].ip_addr, 'cassandra', get_secret_key(cql, 'cassandra'))
    alternator.meta.client.list_tables()
    # Privileged operations also work for the superuser account "cassandra":
    table = alternator.create_table(TableName=unique_table_name(),
        BillingMode='PAY_PER_REQUEST',
        KeySchema=[ {'AttributeName': 'p', 'KeyType': 'HASH' } ],
        AttributeDefinitions=[ {'AttributeName': 'p', 'AttributeType': 'N' } ])
    table.put_item(Item={'p': 42})
    table.get_item(Key={'p': 42})
    table.delete()
    # Create a new role "user2" and make a new connection "alternator2" with it:
    cql.execute("CREATE ROLE user2 WITH PASSWORD = 'user2' AND LOGIN=TRUE")
    alternator2 = get_alternator(servers[0].ip_addr, 'user2', get_secret_key(cql, 'user2'))
    # In the new role, ListTables works, but other privileged operations
    # don't.
    alternator2.meta.client.list_tables()
    with pytest.raises(ClientError, match='AccessDeniedException'):
        alternator2.create_table(TableName=unique_table_name(),
            BillingMode='PAY_PER_REQUEST',
            KeySchema=[ {'AttributeName': 'p', 'KeyType': 'HASH' } ],
            AttributeDefinitions=[ {'AttributeName': 'p', 'AttributeType': 'N' } ])
    # We could further test how GRANT works, but this would be unnecessary
    # repeating of the tests in test/alternator/test_cql_rbac.py.

# Unfortunately by default a Python thread print the exception that kills
# it (e.g., pytest assert failures) but it doesn't propagate the exception
# to the join() - so the overall test doesn't fail. The following ThreadWrapper
# causes join() to rethrow the exception, so the test will fail.
class ThreadWrapper(threading.Thread):
    def run(self):
        try:
            self.ret = self._target(*self._args, **self._kwargs)
        except BaseException as e:
            self.exception = e
    def join(self, timeout=None):
        super().join(timeout)
        if hasattr(self, 'exception'):
            raise self.exception
        return self.ret

# The following tests reproduce issue #13152, where if two schema changes
# are attempted concurrently, one of them may fail with:
#   "Internal server error: service::group0_concurrent_modification
#    (Failed to apply group 0 change due to concurrent modification)."
# We had this problem in six different operations - CreateTable, DeleteTable,
# UpdateTable, TagResource, UntagResource and UpdateTimeToLive - so we have
# several tests (the last three can be tested with almost identical code,
# so they share one parameterized test).
# Each of these tests checks concurrent invocation of just one operation
# (e.g., CreateTable), to allow us to reproduce the missing code in that
# specific operation. We assume that the correct code will use the same
# lock for all operations, so we don't need to test collision of diffent
# operations (e.g., CreateTable and DeleteTable) after we already test that
# CreateTable and DeleteTable each does the locking and retry correctly.
#
# This issue can only be reproduced on a cluster of multiple nodes when
# the operations are sent to different nodes - because a single node
# serializes its own schema modifications. This is why these tests must
# be here, in test/cluster, and not in the single-node test/alternator.

async def test_concurrent_createtable(manager: ManagerClient):
    """A reproducer for issue #13152 for the CreateTable operation:
       concurrent CreateTable operations shouldn't fail "due to concurrent
       "modification".
    """
    servers = await manager.servers_add(3, config=alternator_config)
    # In boto3, "resources", the object returned by get_alternator(), are
    # not thread-safe. However, we will create 3 threads each will write to
    # a different alternators[i], so we're fine.
    alternators = [get_alternator(server.ip_addr) for server in servers]

    # Run the CreateTable operation, once, in each thread. There is no point
    # in running multiple CreateTable operations, since only the very first
    # CreateTable operation (before the table exists) will be slow and have
    # an appreciatable chance of colliding with another concurrent operation.
    # We'll use a barrier to increase the chance that the 3 threads start
    # together and collide - on my test machine, before #15132 was fixed one
    # attempt here fails around 80% of the time, which is good enough to
    # reproduce the bug and test its fix. Nevertheless, we'll run (below)
    # the whole check a "ntries" times in a loop, to bring number of test
    # false-negatives even closer to zero.
    table_name = unique_table_name()
    barrier = threading.Barrier(len(servers), timeout=120)
    def run_op(dynamodb):
        barrier.wait()
        try:
            dynamodb.create_table(TableName=table_name,
                BillingMode='PAY_PER_REQUEST',
                KeySchema=[{'AttributeName': 'p', 'KeyType': 'HASH' }],
                AttributeDefinitions=[{'AttributeName': 'p', 'AttributeType': 'N' }])
        # Expect either a success or a ResourceInUseException.
        # Anything else (e.g., InternalServerError) is a bug
        except ClientError as e:
            assert 'ResourceInUseException' in str(e)
    ntries = 5
    for i in range(ntries):
        threads = [ThreadWrapper(target=run_op, args=[dynamodb]) for dynamodb in alternators]
        for t in threads:
            t.start()
        try:
            for t in threads:
                t.join()
            # If we're here, all the threads were successful, and the
            # test passed. Actually it needs to pass ntries times before
            # we really declare it successful.
        finally:
            barrier.reset()
            # In theory (and in DynamoDB), delete_table() isn't possible
            # until create_table() completed its asynchronous work, so
            # we may need to try delete_table() multiple times.
            timeout = time.time() + 120
            while time.time() < timeout:
                try:
                    alternators[0].meta.client.delete_table(TableName=table_name)
                    break
                except ClientError as ce:
                    if ce.response['Error']['Code'] == 'ResourceInUseException':
                        time.sleep(1)
                        continue
                    elif ce.response['Error']['Code'] == 'ResourceNotFoundException':
                        # The table was never created, probably we had an
                        # exception from the table-creation threads, let's
                        # not add more error messages here.
                        break
                    raise

async def test_concurrent_deletetable(manager: ManagerClient):
    """A reproducer for issue #13152 for the DeleteTable operation:
       concurrent DeleteTable operations shouldn't fail "due to concurrent
       "modification".
    """
    servers = await manager.servers_add(3, config=alternator_config)
    alternators = [get_alternator(server.ip_addr) for server in servers]
    table_name = unique_table_name()
    barrier = threading.Barrier(len(servers), timeout=120)
    def run_op(dynamodb):
        barrier.wait()
        try:
            dynamodb.meta.client.delete_table(TableName=table_name)
        # Expect either a success or a ResourceNotFoundException
        # (indicating another thread deleted the table).
        # Anything else (e.g., InternalServerError) is a bug
        except ClientError as e:
            assert 'ResourceNotFoundException' in str(e)
    ntries = 5
    try:
        for i in range(ntries):
            alternators[0].create_table(TableName=table_name,
                BillingMode='PAY_PER_REQUEST',
                KeySchema=[{'AttributeName': 'p', 'KeyType': 'HASH' }],
                AttributeDefinitions=[{'AttributeName': 'p', 'AttributeType': 'N' }])
            alternators[0].meta.client.get_waiter('table_exists').wait(TableName=table_name)
            threads = [ThreadWrapper(target=run_op, args=[dynamodb]) for dynamodb in alternators]
            for t in threads:
                t.start()
            try:
                for t in threads:
                    t.join()
            finally:
                barrier.reset()
                try:
                    alternators[0].meta.client.delete_table(TableName=table_name)
                except ClientError as e:
                    # If we got ResourceNotFoundException, the table was
                    # already deleted by the threads, that's expected.
                    if not 'ResourceNotFoundException' in str(e):
                        raise
    finally:
        # Delete the table, if an exception above caused us not to do it.
        try:
            alternators[0].meta.client.delete_table(TableName=table_name)
        except ClientError as e:
            if not 'ResourceNotFoundException' in str(e):
                raise

async def test_concurrent_updatetable(manager: ManagerClient):
    """A reproducer for issue #13152 for the UpdateTable operation:
       concurrent UpdateTable operations shouldn't fail "due to concurrent
       "modification".
    """
    servers = await manager.servers_add(3, config=alternator_config)
    alternators = [get_alternator(server.ip_addr) for server in servers]
    table_name = unique_table_name()
    barrier = threading.Barrier(len(servers), timeout=120)
    def run_op(dynamodb):
        barrier.wait()
        try:
            # Pick a slow use case of UpdateTable (adding a GSI) to increase
            # the likelihood of a collision.
            dynamodb.meta.client.update_table(TableName=table_name,
                AttributeDefinitions=[{ 'AttributeName': 'x', 'AttributeType': 'S' }],
                GlobalSecondaryIndexUpdates=[ {  'Create':
                    {  'IndexName': 'hello',
                        'KeySchema': [{ 'AttributeName': 'x', 'KeyType': 'HASH' }],
                        'Projection': { 'ProjectionType': 'ALL' }
                    }}])
        # Expect either a success or an error indicating another thread
        # already added this GSI.
        # Anything else (e.g., InternalServerError) is a bug
        except ClientError as e:
            assert 'GSI hello already exists' in str(e)
    ntries = 5
    try:
        for i in range(ntries):
            alternators[0].create_table(TableName=table_name,
                BillingMode='PAY_PER_REQUEST',
                KeySchema=[{'AttributeName': 'p', 'KeyType': 'HASH' }],
                AttributeDefinitions=[{'AttributeName': 'p', 'AttributeType': 'N' }])
            alternators[0].meta.client.get_waiter('table_exists').wait(TableName=table_name)
            threads = [ThreadWrapper(target=run_op, args=[dynamodb]) for dynamodb in alternators]
            for t in threads:
                t.start()
            try:
                for t in threads:
                    t.join()
            finally:
                barrier.reset()
                alternators[0].meta.client.delete_table(TableName=table_name)
    finally:
        # Delete the table, if an exception above caused us not to do it.
        try:
            alternators[0].meta.client.delete_table(TableName=table_name)
        except ClientError as e:
            if not 'ResourceNotFoundException' in str(e):
                raise

@pytest.mark.parametrize('op', ['TagResource', 'UntagResource', 'UpdateTimeToLive'])
async def test_concurrent_modify_tags(manager: ManagerClient, op):
    """A reproducer for issue #13152 for the TagResource, UntagResource
       and UpdateTimeToLive operation (each one in a separate parametrization
       of the test). Concurrent operations shouldn't fail "due to concurrent
       "modification".
       The name of this test is named after db::modify_tags(), which all
       three of these operations use to implement the change to the table.
    """
    servers = await manager.servers_add(3, config=alternator_config)
    alternators = [get_alternator(server.ip_addr) for server in servers]
    table_name = unique_table_name()
    barrier = threading.Barrier(len(servers), timeout=120)
    def run_op(dynamodb):
        barrier.wait()
        if op == 'TagResource':
            arn = dynamodb.meta.client.describe_table(TableName=table_name)['Table']['TableArn']
            dynamodb.meta.client.tag_resource(ResourceArn=arn, Tags=[{'Key': 'animal', 'Value': 'dog'}])
        elif op == 'UntagResource':
            arn = dynamodb.meta.client.describe_table(TableName=table_name)['Table']['TableArn']
            dynamodb.meta.client.untag_resource(ResourceArn=arn, TagKeys=['animal'])
        elif op == 'UpdateTimeToLive':
            # For the UpdateTimeToLive operation to actually attempt a write
            # (and possibly notice a collision), we need to set Enabled to
            # the opposite of what it is right now. Let's just pick a random
            # boolean - 50% of the time it will do the right thing and
            # we may see the collision.
            try:
                dynamodb.meta.client.update_time_to_live(TableName=table_name,
                    TimeToLiveSpecification={'AttributeName': 'xxx', 'Enabled': bool(random.getrandbits(1))})
            except ClientError as e:
                if not 'TTL is already' in str(e):
                    raise
        else:
            pytest.fail(f'oops, bad op {op}')
    alternators[0].create_table(TableName=table_name,
        BillingMode='PAY_PER_REQUEST',
        KeySchema=[{'AttributeName': 'p', 'KeyType': 'HASH' }],
        AttributeDefinitions=[{'AttributeName': 'p', 'AttributeType': 'N' }])
    alternators[0].meta.client.get_waiter('table_exists').wait(TableName=table_name)
    ntries = 5
    try:
        for i in range(ntries):
            threads = [ThreadWrapper(target=run_op, args=[dynamodb]) for dynamodb in alternators]
            for t in threads:
                t.start()
            try:
                for t in threads:
                    t.join()
            finally:
                barrier.reset()
    finally:
        alternators[0].meta.client.delete_table(TableName=table_name)

async def nodes_with_data(manager, ks, cf, host):
    """Retrieves a set of node uuids which contain *any* data for the given
       table. If the table uses tablets, we use the system.tablets (via
       the convenience function get_all_tablet_replicas()). But if the table
       uses vnodes, we use a REST API request /storage_service/tokens_endpoint
       which returns the primary node for each token (with vnodes, if a node
       has any data at all, it is also primary for some of the data).
       The information is retrieved using requests to the given "host",
       which can be any live node.
    """
    r = await get_all_tablet_replicas(manager, host, ks, cf)
    if r:
        # If table uses tablets it will have a non-empty list of tablets (r)
        # and we return it here.
        return { item[0] for entry in r for item in entry.replicas }
    else:
        # Otherwise, the table uses vnodes. Use the REST API that only
        # makes sense with vnodes. Convert the host IP addresses that this
        # API returns to uuids like we have in the system tables
        j = await manager.api.client.get_json('/storage_service/tokens_endpoint', host=host.ip_addr)
        return { await manager.api.get_host_id(entry['value']) for entry in j }

@pytest.mark.parametrize("tablets", [True, False])
@pytest.mark.asyncio
async def test_zero_token_node_load_balancer(manager, tablets):
    """Test that a zero-token node (a.k.a. coordinator-only or proxy node),
       can be used as an Alternator server-side load balancer as proposed in
       issue #6527. We set up a cluster with four ordinary nodes (one DC and
       one rack), and a fifth node which doesn't have any data (a zero-token
       node), and make different Alternator requests (CreateTable, PutItem,
       GetItem) to this data-less fifth node, and they should work. Finally
       we verify that the fifth node really does not have any data (and
       wasn't just created as a normal data-holding node).
       Because the implementation of zero-token nodes is very different
       for the tablets and vnodes cases, this test has two parametrized
       versions - tablets=True and tablets=False.
    """
    if tablets:
        tags = [{'Key': 'experimental:initial_tablets', 'Value': '0'}]
    else:
        tags = [{'Key': 'experimental:initial_tablets', 'Value': 'none'}]
    # Start a cluster with 4 nodes. Alternator uses RF=3, so with 4 nodes
    # the assignment of data (tablets or vnodes) to nodes isn't trivial,
    # which will allow us to check that non-trivial request forwarding works.
    servers = await manager.servers_add(4, config=alternator_config)
    # Add a fifth node, with zero tokens (no data), by setting join_ring=false:
    zero_token_server = await manager.server_add(config=alternator_config | {'join_ring': False})

    # Get an Alternator connection to the zero-token node:
    alternator = get_alternator(zero_token_server.ip_addr)

    # Create a new table, write 10 different items to it and then read them
    # back - doing all of this through the zero-token node:
    table = alternator.create_table(TableName=unique_table_name(),
        Tags=tags,
        BillingMode='PAY_PER_REQUEST',
        KeySchema=[
            {'AttributeName': 'p', 'KeyType': 'HASH' },
        ],
        AttributeDefinitions=[
            {'AttributeName': 'p', 'AttributeType': 'N' },
        ])
    items = [{'p': i, 'x': f'hello {i}'} for i in range(10)]
    for item in items:
        table.put_item(Item=item)
    for item in items:
        assert item == table.get_item(Key={'p': item['p']}, ConsistentRead=True)['Item']
    # Verify that the zero-token node is really "zero-token", i.e., does not
    # have any data for our table. The nodes_with_data() function returns
    # the list of node uuids which contain *any* data for the given table -
    # we want this to be just the first four nodes in "servers", not the
    # fifth node zero_token_server.
    expected = { await manager.get_host_id(s.server_id) for s in servers }
    got = await nodes_with_data(manager, 'alternator_'+table.name, table.name, zero_token_server)
    assert got == expected
    table.delete()

@pytest.mark.xfail(reason="#16261")
async def test_alternator_concurrent_rmw_same_partition_different_server(manager: ManagerClient):
    """A reproducer for issue #16261: When sending RMW (read-modify-write)
       operations to the same partition (different item) on different server
       nodes (coordinators), our LWT implementation can reach an
       "uncertainty" situation where it doesn't know whether the update
       succceeded or passed, and returns a failure (InternalServerError)
       almost immediately, not after a cas_contention_timeout_in_ms timeout
       (1 second).
    """
    servers = await manager.servers_add(3, config=alternator_config)
    alternator = get_alternator(servers[0].ip_addr)
    ips = [server.ip_addr for server in await manager.running_servers()]
    table = alternator.create_table(TableName=unique_table_name(),
        BillingMode='PAY_PER_REQUEST',
        KeySchema=[
            {'AttributeName': 'p', 'KeyType': 'HASH' },
            {'AttributeName': 'c', 'KeyType': 'RANGE' },
        ],
        AttributeDefinitions=[
            {'AttributeName': 'p', 'AttributeType': 'N' },
            {'AttributeName': 'c', 'AttributeType': 'N' },
        ])

    # All threads write to one partition 1, each to a different item
    # in that partition (its clustering key is the thread's number).
    # Each update gets sent to a random node (we have 3 nodes in ips).
    nthreads = 3
    def run_rmw(i):
        rand = random.Random()
        rand.seed(i)
        alternators = [get_alternator(ip) for ip in ips]
        # In about 1/10 runs, just one write from each thread is enough
        # to elicit the error. But if I want to get the error in almost
        # every run, I need to repeat the write more times, until two
        # of the writes collide and cause the bug.
        for n in range(150):
            alternator_i = rand.randrange(len(alternators))
            alternator = alternators[alternator_i]
            tbl = alternator.Table(table.name)
            start = time.time()
            try:
                tbl.update_item(Key={'p': 1, 'c': i},
                    UpdateExpression='SET v = if_not_exists(v, :init) + :incr',
                    ExpressionAttributeValues={':init': 0, ':incr': 1})
            except ClientError:
                # The "raise" will cause this thread to fail, and eventually
                # the join() and therefore the whole test will fail. We also
                # print the time it took for the failure, because it
                # demonstrates that issue #16261 involves an immediate
                # error (in less than 20ms), NOT a normal timeout.
                print(f"In incrementing 1,{i} on node {alternator_i}: error after {time.time()-start}")
                raise

    threads = [ThreadWrapper(target=run_rmw, args=(i,)) for i in range(nthreads)]
    for t in threads:
        t.start()
    try:
        for t in threads:
            t.join()
    finally:
        table.delete()
