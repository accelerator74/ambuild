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
import os
import util
import nodetypes
from frontend.cpp import DetectCompiler
from frontend.amb2 import dbcreator
from frontend.amb2 import graphbuilder
from frontend import base_gen

class Generator(base_gen.Generator):
  def __init__(self, sourcePath, buildPath, options, args):
    super(Generator, self).__init__(sourcePath, buildPath, options, args)
    self.cacheFolder = os.path.join(buildPath, '.ambuild2')
    self.graph = graphbuilder.GraphBuilder()

  def preGenerate(self):
    self.cleanPriorBuild()

  def cleanPriorBuild(self):
    if os.path.isdir(self.cacheFolder):
      util.RemoveFolderAndContents(self.cacheFolder)
    os.mkdir(self.cacheFolder)

  def addCxxTasks(self, cx, binary):
    folder = os.path.join(cx.buildFolder, binary.name)
    folderNode = self.graph.generateFolder(folder)

    binNode = self.graph.addOutput(path=binary.outputFile)
    linkCmd = self.graph.addCommand(type=nodetypes.Command,
                                    folder=folderNode,
                                    data=binary.argv)
    self.graph.addDependency(binNode, linkCmd)

    if binary.pdbFile:
      pdbNode = self.graph.addOutput(path=binary.pdbFile)
      self.graph.addDependency(pdbNode, linkCmd)
    else:
      pdbNode = None

    # Find dependencies
    for item in binary.compiler.linkflags:
      if type(item) is str:
        continue
      self.graph.addDependency(linkCmd, item.node)

    for item in binary.compiler.postlink:
      if type(item) is str:
        node = self.graph.depNodeForPath(item)
      else:
        node = item.node
      self.graph.addDependency(linkCmd, node)

    for objfile in binary.objfiles:
      srcNode = self.graph.addSource(path=objfile.sourceFile)
      cxxData = {
        'argv': objfile.argv,
        'type': binary.linker.behavior
      }
      objNode = self.graph.addOutput(path=objfile.outputFile)
      cxxNode = self.graph.addCommand(type=nodetypes.Cxx,
                                      folder=folderNode,
                                      data=cxxData)
      self.graph.addDependency(cxxNode, srcNode)
      self.graph.addDependency(objNode, cxxNode)
      self.graph.addDependency(linkCmd, objNode)

    return binNode, pdbNode

  def postGenerate(self):
    dbpath = os.path.join(self.cacheFolder, 'graph')
    with dbcreator.Database(dbpath) as database:
      database.createTables()
      database.exportGraph(self.graph)
    self.saveVars()
    self.generateBuildFile()

  def AddSource(self, context, source_path):
    return self.graph.addSource(source_path)

  def AddSymlink(self, context, source, output_path):
    folder, name = os.path.split(output_path)
    return self.graph.addSymLink(context, source, folder, name)

  def AddFolder(self, context, folder):
    folder = os.path.join(context.buildFolder, folder)
    return self.graph.generateFolder(folder)

  def AddCopy(self, context, source, folder):
    return self.graph.addCopy(context, source, folder)

  def generateBuildFile(self):
    with open(os.path.join(self.buildPath, 'build.py'), 'w') as fp:
      fp.write("""
# vim set: ts=8 sts=2 sw=2 tw=99 et:
import sys
sys.path.append('/home/dvander/alliedmodders/ambuild/ambuild2')
import run

if not run.Build("{build}"):
  sys.exit(1)
""".format(build=self.buildPath))

  def saveVars(self):
    vars = {
      'sourcePath': self.sourcePath,
      'buildPath': self.buildPath
    }
    with open(os.path.join(self.cacheFolder, 'vars'), 'wb') as fp:
      util.pickle.dump(vars, fp)
