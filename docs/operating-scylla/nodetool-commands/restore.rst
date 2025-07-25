================
Nodetool restore
================

**restore** - Load SSTables from a designated bucket in object store into a specified keyspace or table

The status of a restore operation is retained for a period defined by ``user_task_ttl``,
allowing you to query the status even after the operation completes.
You can configure the TTL duration using the :doc:`nodetool tasks user-ttl </operating-scylla/nodetool-commands/tasks/user-ttl>` command.

When you run a restore operation, it always executes as a background task. You have two ways to interact with this task:

* **Without the ``--nowait`` flag (default)**: The command waits for the restore operation to complete and returns the final status. This approach relies on the ``user_task_ttl`` setting. If ``user_task_ttl`` is set too low (especially if set to 0), the task record might be removed before the status can be checked, potentially causing the command to fail in reporting the result.

* **With the ``--nowait`` flag**: The command immediately returns a task ID without waiting for the operation to complete. You can use this task ID with the :doc:`nodetool tasks </operating-scylla/nodetool-commands/tasks/index>` command to monitor the progress of the restore operation or to cancel it if needed. The task information remains available for the duration specified by ``user_task_ttl`` after completion.

Syntax
------

.. code-block:: console

   nodetool [(-h <host> | --host <host>)] [(-p <port> | --port <port>)]
               --endpoint <endpoint> --bucket <bucket>
               --prefix <prefix>
               --keyspace <keyspace>
               --table <table>
               [--nowait]
               [--scope <scope>]
               [--sstables-file-list <file>]
               <sstables>...

Example
-------

.. code-block:: console

   nodetool restore --endpoint s3.us-east-2.amazonaws.com  --bucket bucket-foo --prefix ks/cf/24601 --keyspace ks --table cf \
     scylla/ks/cf/34/me-3gdq_0bki_2dy4w2gqj6hoso4mw1-big-TOC.txt \
     scylla/ks/cf/34/me-3gdq_0bki_2dipc1ysb2x2a3btgh-big-TOC.txt \
     scylla/ks/cf/42/me-3gdq_0bki_2s3e829t3gyq994yjl-big-TOC.txt


Options
-------

* ``-h <host>`` or ``--host <host>`` - Node hostname or IP address.
* ``--endpoint`` - Name of the configured object storage endpoint to load SSTables from.
  This should be configured as per :ref:`the object storage configuration instructions <object-storage-configuration>`.
* ``--bucket`` - Name of the bucket to load SSTables from
* ``--prefix`` - The share prefix for object keys of backed up SSTables
* ``--keyspace`` - Name of the keyspace to load SSTables into
* ``--table`` - Name of the table to load SSTables into
* ``--nowait`` - Don't wait on the restore process
* ``--scope <scope>`` - Use specified load-and-stream scope
* ``--sstables-file-list <file>`` - restore the sstables listed in the given <file>. the list should be new-line seperated.
* ``<sstables>`` - Remainder of keys of the TOC (Table of Contents) components of SSTables to restore, relative to the specified prefix

The `scope` parameter describes the subset of cluster nodes where you want to load data:

* `node` - On the local node.
* `rack` - On the local rack.
* `dc` - In the datacenter (DC) where the local node lives.
* `all` (default) - Everywhere across the cluster.

`--sstables-file-list <file>` and `<sstable>` can be combined together, `nodetool restore` will attempt to restore the combined list. duplicates are _not_ removed

To fully restore a cluster, you should combine the ``scope`` parameter with the correct list of
SStables to restore to each node.
On one extreme, one node is given all SStables with the scope ``all``; on the other extreme, all
nodes are restoring only their own SStables with the scope ``node``. In between, you can choose
a subset of nodes to restore only SStables that belong to the rack or DC.

See also

:doc:`Nodetool backup </operating-scylla/nodetool-commands/backup/>`

.. include:: nodetool-index.rst
