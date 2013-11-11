# vim: set ts=8 sts=2 sw=2 tw=99 et:
#
# This file is part of AMBuild.
# 
# AMBuild is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# 
# AMBuild is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
# 
# You should have received a copy of the GNU General Public License
# along with AMBuild. If not, see <http://www.gnu.org/licenses/>.
import errno
import os, sys
import sqlite3
from ambuild2 import util
from ambuild2 import nodetypes
from ambuild2.nodetypes import Entry
import traceback

GroupPrefix = '//group/./'

class Database(object):
  def __init__(self, path):
    self.path = path
    self.cn = None
    self.node_cache_ = {}
    self.path_cache_ = {}

  def connect(self):
    assert not self.cn
    self.cn = sqlite3.connect(self.path)
    self.cn.execute("PRAGMA journal_mode = WAL;")

  def close(self):
    if self.cn:
      self.cn.close()
    self.cn = None

  def __enter__(self):
    self.connect()
    return self

  def __exit__(self, type, value, traceback):
    self.close()

  def commit(self):
    self.cn.commit()

  def flush_caches(self):
    self.node_cache_ = {}
    self.path_cache_ = {}

  def create_tables(self):
    queries = [
      "create table if not exists nodes(            \
        id integer primary key autoincrement,       \
        type varchar(4) not null,                   \
        stamp real not null default 0.0,            \
        dirty int not null default 0,               \
        generated int not null default 0,           \
        path text,                                  \
        folder int,                                 \
        data blob                                   \
      )",

      # The edge table stores links that are specified by the build scripts;
      # this table is essentially immutable (except for reconfigures).
      "create table if not exists edges(          \
        outgoing int not null,                    \
        incoming int not null,                    \
        unique (outgoing, incoming)               \
      )",

      # The weak edge table stores links that are specified by build scripts,
      # but only to enforce ordering. They do not propagate damage or updates.
      "create table if not exists weak_edges(     \
        outgoing int not null,                    \
        incoming int not null,                    \
        unique (outgoing, incoming)               \
      )",

      # The dynamic edge table stores edges that are discovered as a result of
      # executing a command; for example, a |cp *| or C++ #includes.
      "create table if not exists dynamic_edges(  \
        outgoing int not null,                    \
        incoming int not null,                    \
        unique (outgoing, incoming)               \
      )",

      # List of nodes which trigger a reconfigure.
      "create table if not exists reconfigure(    \
        stamp real not null default 0.0,          \
        path text unique                          \
      )",

      "create index if not exists outgoing_edge on edges(outgoing)",
      "create index if not exists incoming_edge on edges(incoming)",
      "create index if not exists weak_outgoing_edge on weak_edges(outgoing)",
      "create index if not exists weak_incoming_edge on weak_edges(incoming)",
      "create index if not exists dyn_outgoing_edge on dynamic_edges(outgoing)",
      "create index if not exists dyn_incoming_edge on dynamic_edges(incoming)",
    ]
    for query in queries:
      self.cn.execute(query)
    self.cn.commit()

  def add_folder(self, parent, path):
    assert path not in self.path_cache_
    assert not os.path.isabs(path)
    assert os.path.normpath(path) == path

    # We don't use the generated bit for folders right now.
    return self.add_file(nodetypes.Mkdir, path, False, parent)

  def add_output(self, folder_entry, path):
    assert path not in self.path_cache_
    assert not os.path.isabs(path)
    assert not folder_entry or os.path.split(path)[0] == folder_entry.path

    return self.add_file(nodetypes.Output, path, False, folder_entry)

  def find_or_add_source(self, path):
    node = self.query_path(path)
    if node:
      assert node.type == nodetypes.Source
      return node

    return self.add_source(path)

  def add_source(self, path, generated=False):
    assert path not in self.path_cache_
    assert os.path.isabs(path)

    return self.add_file(nodetypes.Source, path, generated)

  def add_file(self, type, path, generated, folder_entry = None):
    if folder_entry:
      folder_id = folder_entry.id
    else:
      folder_id = None

    query = "insert into nodes (type, generated, path, folder) values (?, ?, ?, ?)"

    cursor = self.cn.execute(query, (type, int(generated), path, folder_id))
    row = (type, 0, 1, 1, path, folder_entry, None)
    return self.import_node(
      id=cursor.lastrowid,
      row=row
    )

  def find_group(self, name):
    path = GroupPrefix + name
    return self.query_path(path)

  def add_group(self, name):
    path = GroupPrefix + name
    return self.add_file(nodetypes.Group, path, False)

  def update_command(self, entry, type, folder, data, refactoring):
    if not data:
      blob = None
    else:
      blob = util.BlobType(util.CompatPickle(data))

    if entry.type == type and entry.folder == folder and entry.blob == data:
      return False

    if refactoring:
      util.con_err(util.ConsoleRed, 'Command changed! \n',
                   util.ConsoleRed, 'Old: ',
                   util.ConsoleBlue, entry.format(),
                   util.ConsoleNormal)
      entry.type = type
      entry.folder = folder
      entry.blob = blob
      util.con_err(util.ConsoleRed, 'New: ',
                   util.ConsoleBlue, entry.format(),
                   util.ConsoleNormal)
      raise Exception('Refactoring error: command changed')

    if not folder:
      folder_id = None
    else:
      folder_id = folder.id

    query = """
      update nodes
      set
        type = ?,
        folder = ?,
        data = ?,
        dirty = ?
      where id = ?
    """
    self.cn.execute(query, (type, folder_id, blob, 1, entry.id))
    entry.type = type
    entry.folder = folder
    entry.blob = blob
    entry.dirty = True
    return True

  def add_command(self, type, folder, data):
    if not data:
      blob = None
    else:
      blob = util.BlobType(util.CompatPickle(data))
    if not folder:
      folder_id = None
    else:
      folder_id = folder.id

    query = "insert into nodes (type, folder, data, dirty) values (?, ?, ?, ?)"
    cursor = self.cn.execute(query, (type, folder_id, blob, 1))

    entry = Entry(
      id=cursor.lastrowid,
      type=type,
      path=None,
      blob=data,
      folder=folder,
      stamp=0,
      dirty=True,
      generated=False
    )
    self.node_cache_[entry.id] = entry
    return entry

  def add_weak_edge(self, from_entry, to_entry):
    query = "insert into weak_edges (outgoing, incoming) values (?, ?)"
    self.cn.execute(query, (to_entry.id, from_entry.id))
    if to_entry.weak_inputs:
      to_entry.weak_inputs.add(from_entry)

  def add_strong_edge(self, from_entry, to_entry):
    query = "insert into edges (outgoing, incoming) values (?, ?)"
    self.cn.execute(query, (to_entry.id, from_entry.id))
    if to_entry.strong_inputs:
      to_entry.strong_inputs.add(from_entry)
    if from_entry.outgoing:
      from_entry.outgoing.add(to_entry)

  def add_dynamic_edge(self, from_entry, to_entry):
    query = "insert into dynamic_edges (outgoing, incoming) values (?, ?)"
    self.cn.execute(query, (to_entry.id, from_entry.id))
    if to_entry.dynamic_inputs:
      to_entry.dynamic_inputs.add(from_entry)
    if from_entry.outgoing:
      from_entry.outgoing.add(to_entry)

  def drop_dynamic_edge(self, from_entry, to_entry):
    query = "delete from dynamic_edges edges where outgoing = ? and incoming = ?"
    self.cn.execute(query, (to_entry.id, from_entry.id))
    if to_entry.dynamic_inputs:
      to_entry.dynamic_inputs.remove(from_entry)
    if from_entry.outgoing:
      from_entry.outgoing.remove(to_entry)

  def drop_weak_edge(self, from_entry, to_entry):
    query = "delete from weak_edges edges where outgoing = ? and incoming = ?"
    self.cn.execute(query, (to_entry.id, from_entry.id))
    if to_entry.weak_inputs:
      to_entry.weak_inputs.remove(from_entry)

  def drop_strong_edge(self, from_entry, to_entry):
    query = "delete from edges where outgoing = ? and incoming = ?"
    self.cn.execute(query, (to_entry.id, from_entry.id))
    if to_entry.strong_inputs:
      to_entry.strong_inputs.remove(from_entry)
    if from_entry.outgoing:
      from_entry.outgoing.remove(to_entry)

  def query_node(self, id):
    if id in self.node_cache_:
      return self.node_cache_[id]

    query = "select type, stamp, dirty, generated, path, folder, data from nodes where id = ?"
    cursor = self.cn.execute(query, (id,))
    return self.import_node(id, cursor.fetchone())

  def query_path(self, path):
    if path in self.path_cache_:
      return self.path_cache_[path]

    query = """
      select type, stamp, dirty, generated, path, folder, data, id
      from nodes
      where path = ?
    """
    cursor = self.cn.execute(query, (path,))
    row = cursor.fetchone()
    if not row:
      return None

    return self.import_node(row[7], row)

  def import_node(self, id, row):
    assert id not in self.node_cache_

    if not row[5]:
      folder = None
    elif type(row[5]) is nodetypes.Entry:
      folder = row[5]
    else:
      folder = self.query_node(row[5])
    if not row[6]:
      blob = None
    else:
      blob = util.Unpickle(row[6])

    node = Entry(id=id,
                 type=row[0],
                 path=row[4],
                 blob=blob,
                 folder=folder,
                 stamp=row[1],
                 dirty=row[2],
                 generated=bool(row[3]))
    self.node_cache_[id] = node
    if node.path:
      assert node.path not in self.path_cache_
      self.path_cache_[node.path] = node
    return node

  def query_strong_outgoing(self, node):
    # Not cached yet.
    outgoing = set()
    query = "select outgoing from edges where incoming = ?"
    for outgoing_id, in self.cn.execute(query, (node.id,)):
      entry = self.query_node(outgoing_id)
      outgoing.add(entry)
    return outgoing

  def query_outgoing(self, node):
    if node.outgoing:
      return node.outgoing

    node.outgoing = set()

    query = "select outgoing from edges where incoming = ?"
    for outgoing_id, in self.cn.execute(query, (node.id,)):
      entry = self.query_node(outgoing_id)
      node.outgoing.add(entry)

    query = "select outgoing from dynamic_edges where incoming = ?"
    for outgoing_id, in self.cn.execute(query, (node.id,)):
      entry = self.query_node(outgoing_id)
      node.outgoing.add(entry)

    return node.outgoing

  def query_weak_inputs(self, node):
    if node.weak_inputs:
      return node.weak_inputs

    query = "select incoming from weak_edges where outgoing = ?"
    node.weak_inputs = set()
    for incoming_id, in self.cn.execute(query, (node.id,)):
      incoming = self.query_node(incoming_id)
      node.weak_inputs.add(incoming)

    return node.weak_inputs

  def query_strong_inputs(self, node):
    if node.strong_inputs:
      return node.strong_inputs

    query = "select incoming from edges where outgoing = ?"
    node.strong_inputs = set()
    for incoming_id, in self.cn.execute(query, (node.id,)):
      incoming = self.query_node(incoming_id)
      node.strong_inputs.add(incoming)

    return node.strong_inputs

  def query_dynamic_inputs(self, node):
    if node.dynamic_inputs:
      return node.dynamic_inputs

    query = "select incoming from dynamic_edges where outgoing = ?"
    node.dynamic_inputs = set()
    for incoming_id, in self.cn.execute(query, (node.id,)):
      incoming = self.query_node(incoming_id)
      node.dynamic_inputs.add(incoming)

    return node.dynamic_inputs

  def mark_dirty(self, entry):
    query = "update nodes set dirty = 1 where id = ?"
    self.cn.execute(query, (entry.id,))
    entry.dirty |= nodetypes.KnownDirty

  def unmark_dirty(self, entry, stamp=None):
    query = "update nodes set dirty = 0, stamp = ? where id = ?"
    if not stamp:
      if entry.isCommand():
        stamp = 0.0
      else:
        try:
          stamp = os.path.getmtime(entry.path)
        except:
          traceback.print_exc()
          util.con_err(
            util.ConsoleRed,
            'Could not unmark file as dirty; leaving dirty.',
            util.ConsoleNormal
          )

    self.cn.execute(query, (stamp, entry.id))
    entry.dirty = False
    entry.stamp = stamp

  # Query all mkdir nodes.
  def query_mkdir(self, aggregate):
    query = """
      select type, stamp, dirty, generated, path, folder, data, id
      from nodes
      where type == 'mkd'
    """
    for row in self.cn.execute(query):
      id = row[7]
      node = self.import_node(id, row)
      aggregate(node)

  # Intended to be called before any nodes are imported.
  def query_known_dirty(self, aggregate):
    query = """
      select type, stamp, dirty, generated, path, folder, data, id
      from nodes
      where dirty = 1
      and type != 'mkd'
    """
    for row in self.cn.execute(query):
      id = row[7]
      node = self.import_node(id, row)
      aggregate(node)

  # Query all nodes that are not dirty, but need to be checked. Intended to
  # be called after query_dirty, and returns a mutually exclusive list.
  def query_maybe_dirty(self, aggregate):
    query = """
      select type, stamp, dirty, generated, path, folder, data, id
      from nodes
      where dirty = 0
      and (type == 'src' or type == 'out' or type == 'cpa')
    """
    for row in self.cn.execute(query):
      id = row[7]
      node = self.import_node(id, row)
      aggregate(node)

  def query_commands(self, aggregate):
    query = """
      select type, stamp, dirty, generated, path, folder, data, id
      from nodes
      where (type != 'src' and
             type != 'out' and
             type != 'grp' and
             type != 'mkd')
    """
    for row in self.cn.execute(query):
      id = row[7]
      node = self.import_node(id, row)
      aggregate(node)

  def drop_entry(self, entry):
    query = "delete from nodes where id = ?"
    self.cn.execute(query, (entry.id,))

    query = "delete from edges where incoming = ? or outgoing = ?"
    self.cn.execute(query, (entry.id, entry.id))

    query = "delete from dynamic_edges where incoming = ? or outgoing = ?"
    self.cn.execute(query, (entry.id, entry.id))

    query = "delete from weak_edges where incoming = ? or outgoing = ?"
    self.cn.execute(query, (entry.id, entry.id))

    del self.node_cache_[entry.id]

  def drop_folder(self, entry):
    assert entry.type == nodetypes.Mkdir
    assert not os.path.isabs(entry.path)

    if os.path.exists(entry.path):
      util.con_out(
        util.ConsoleHeader,
        'Removing old folder: ',
        util.ConsoleBlue,
        '{0}'.format(entry.path),
        util.ConsoleNormal
      )

    try:
      os.rmdir(entry.path)
    except OSError as exn:
      if exn.errno != errno.ENOENT:
        util.con_err(
          util.ConsoleRed,
          'Could not remove folder: ',
          util.ConsoleBlue,
          '{0}'.format(entry.path),
          util.ConsoleNormal,
          '\n',
          util.ConsoleRed,
          '{0}'.format(exn),
          util.ConsoleNormal
        )
        raise

    cursor = self.cn.execute("select count(*) from nodes where folder = ?", (entry.id,))
    amount = cursor.fetchone()[0]
    if amount > 0:
      util.con_err(
        util.ConsoleRed,
        'Database id ',
        util.ConsoleBlue,
        '{0} '.format(entry.id),
        util.ConsoleRed,
        'is about to be deleted, but is still in use as a folder!',
        util.ConsoleNormal
      )
      raise Exception('folder still in use!')

    self.drop_entry(entry)

  def drop_output(self, output):
    assert output.type == nodetypes.Output
    assert not os.path.isabs(output.path)

    if os.path.exists(output.path):
      util.con_out(
        util.ConsoleHeader,
        'Removing old output: ',
        util.ConsoleBlue,
        '{0}'.format(output.path),
        util.ConsoleNormal
      )

    try:
      os.unlink(output.path)
    except OSError as exn:
      if exn.errno != errno.ENOENT:
        util.con_err(
          util.ConsoleRed,
          'Could not remove file: ',
          util.ConsoleBlue,
          '{0}'.format(output.path),
          util.ConsoleNormal,
          '\n',
          util.ConsoleRed,
          '{0}'.format(exn),
          util.ConsoleNormal
        )
        raise
    self.drop_entry(output)

  def drop_command(self, cmd_entry):
    for output in self.query_outgoing(cmd_entry):
      # Commands should never have dynamic outgoing edges, FWIW.
      assert output.type == nodetypes.Output
      self.drop_output(output)
    self.drop_entry(cmd_entry)

  def add_or_update_script(self, path):
    stamp = os.path.getmtime(path)
    query = "insert or replace into reconfigure (path, stamp) values (?, ?)"
    self.cn.execute(query, (path, stamp))

  def query_scripts(self, aggregate):
    query = "select rowid, path, stamp from reconfigure"
    for rowid, path, stamp in self.cn.execute(query):
      aggregate(rowid, path, stamp)

  def query_groups(self, aggregate):
    query = """
      select type, stamp, dirty, generated, path, folder, data, id
      from nodes
      where type == 'grp'
    """
    for row in self.cn.execute(query):
      id = row[7]
      entry = self.import_node(id, row)
      aggregate(entry)

  def drop_script(self, path):
    self.cn.execute("delete from reconfigure where path = ?", (path,))

  def drop_group(self, group):
    self.drop_entry(group)

  def printGraph(self):
    # Find all mkdir nodes.
    query = "select path from nodes where type = 'mkd'"
    for path, in self.cn.execute(query):
      print(' : mkdir \"' + path + '\"')
    # Find all other nodes that have no outgoing edges.
    query = "select id from nodes where id not in (select incoming from edges) and type != 'mkd'"
    for id, in self.cn.execute(query):
      node = self.query_node(id)
      self.printGraphNode(node, 0)

  def printGraphNode(self, node, indent):
    print(('  ' * indent) + ' - ' + node.format())

    for incoming in self.query_strong_inputs(node):
      self.printGraphNode(incoming, indent + 1)
    for incoming in self.query_dynamic_inputs(node):
      self.printGraphNode(incoming, indent + 1)
