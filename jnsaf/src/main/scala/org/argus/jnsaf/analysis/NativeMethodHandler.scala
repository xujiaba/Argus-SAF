/*
 * Copyright (c) 2017. Fengguo Wei and others.
 * All rights reserved. This program and the accompanying materials
 * are made available under the terms of the Eclipse Public License v1.0
 * which accompanies this distribution, and is available at
 * http://www.eclipse.org/legal/epl-v10.html
 *
 * Detailed contributors are listed in the CONTRIBUTOR.md
 */

package org.argus.jnsaf.analysis

import hu.ssh.progressbar.console.ConsoleProgressBar
import org.argus.amandroid.core.ApkGlobal
import org.argus.amandroid.core.parser.ComponentInfo
import org.argus.jawa.alir.util.ExplicitValueFinder
import org.argus.jawa.ast.CallStatement
import org.argus.jawa.core._
import org.argus.jawa.core.util._
import org.argus.jnsaf.serialization.DataCommunicator

object NativeMethodHandler {
  def getJNIFunctionName(global: Global, sig: Signature): String = {
    val clazz = global.getClassOrResolve(sig.classTyp)
    val overload = clazz.getMethodsByName(sig.methodName).size > 1
    getJNIFunctionName(sig, overload)
  }

  def getJNIFunctionName(sig: Signature, overload: Boolean): String = {
    val classNamePart = sig.classTyp.jawaName.replaceAll("_", "_1").replace('.', '_')
    val methodNamePart = sig.methodName.replaceAll("_", "_1")
    if (overload) {
      val paramPart = sig.getParameterTypes.map { typ =>
        val param = JavaKnowledge.formatTypeToSignature(typ).replaceAll("_", "_1").replaceAll("/", "_").replaceAll(";", "_2").replaceAll("\\[", "_3")
        val sb = new StringBuilder
        param.foreach { ch =>
          if (ch >= 128) { // unicode
            sb.append(s"_0${Integer.toHexString(ch)}")
          } else {
            sb.append(ch)
          }
        }
        sb.toString
      }.mkString("")
      s"Java_${classNamePart}_${methodNamePart}__$paramPart"
    } else {
      s"Java_${classNamePart}_$methodNamePart"
    }
  }
}

/**
  * Created by fgwei on 4/27/17.
  */

class NativeMethodHandler(bridge: NativeDroidBridge) {

  import NativeMethodHandler._

  final val LOAD_LIBRARY: Signature = new Signature("Ljava/lang/System;.loadLibrary:(Ljava/lang/String;)V")

  /**
    * If there are no so file found, assume any so file could be the candidate
    */
  val nativeMethodSoMap: MMap[Signature, String] = mmapEmpty

  /**
    * Don't do this for large apps as it will try to resolve all the class.
    *
    * @param global Global
    */
  def buildNativeMethodMapping(global: Global): Unit = {
    val haveNativeClasses = global.getApplicationClassCodes.filter{ case (_, sf) => sf.code.contains("NATIVE")}.keySet
    val progressBar = ConsoleProgressBar.on(System.out).withFormat("[:bar] :percent% :elapsed Left: :remain")
    ProgressBarUtil.withProgressBar("Build native method to so file mapping...", progressBar)(haveNativeClasses, resolveNativeToSoMap(global))
  }

  private def resolveNativeToSoMap(global: Global): JawaType => Unit = { typ =>
    val clazz = global.getClassOrResolve(typ)
    if (clazz.isApplicationClass) {
      val nm = clazz.getDeclaredMethods.filter(_.isNative)
      if (nm.nonEmpty) {
        val soNames = resolveLoadLibrary(global, typ)
        nm.foreach { n =>
          val jniFuncName = getJNIFunctionName(global, n.getSignature)
          soNames.foreach { so =>
            if(bridge.hasSymbol(so, jniFuncName)) {
              nativeMethodSoMap(n.getSignature) = so
            }
          }
        }
      }
    }
  }

  private def resolveLoadLibrary(global: Global, typ: JawaType): ISet[String] = {
    val res: MSet[String] = msetEmpty
    val clazz = global.getClassOrResolve(typ)
    clazz.getDeclaredMethods.foreach { method =>
      if (clazz.isApplicationClass && method.isConcrete) {
        method.getBody.resolvedBody.locations foreach { l =>
          l.statement match {
            case cs: CallStatement =>
              if (cs.signature == LOAD_LIBRARY) {
                res ++= ExplicitValueFinder.findExplicitLiteralForArgs(method, l, cs.arg(0)).filter(_.isString).map(lit => s"lib${lit.getString}.so")
              }
            case _ =>
          }
        }
      }
    }
    res.toSet
  }

  def genSummary(apk: ApkGlobal, sig: Signature): (String, String) = {
    val soOpt: Option[String] = nativeMethodSoMap.get(sig) match {
      case a@Some(_) => a
      case None =>
        resolveNativeToSoMap(apk)(sig.getClassType)
        nativeMethodSoMap.get(sig)
    }
    soOpt match {
      case Some(soName) =>
        bridge.getSoFilePath(apk.model.layout.outputSrcUri, soName) match {
          case Some(soPath) =>
            return bridge.genSummary(soPath, getJNIFunctionName(apk, sig), sig, DataCommunicator.serializeParameters(sig))
          case None =>
        }
      case None =>
    }
    ("", s"`${sig.signature}`:;")
  }

  def analyseNativeActivity(apk: ApkGlobal, native_ac: ComponentInfo): Unit = {
    val soNames: ISet[String] = native_ac.meta_datas.get("android.app.lib_name") match {
      case Some(libname) =>
        Set(s"lib$libname.so")
      case None =>
        resolveLoadLibrary(apk, native_ac.compType)
    }
    var soPaths: IList[String] = soNames.flatMap { soName =>
      bridge.getSoFilePath(apk.model.layout.outputSrcUri, soName)
    }.toList
    if(soPaths.isEmpty) {
      soPaths = bridge.getAllSoFilePath(apk.model.layout.outputSrcUri)
    }
    val customEntry: Option[String] = native_ac.meta_datas.get("android.app.func_name")
    soPaths.foreach { soPath =>
      if(bridge.hasNativeActivity(soPath, customEntry)) {
        println(soPath)
        bridge.analyseNativeActivity(soPath, customEntry)
        return
      }
    }
  }

  def statisticsResolve(apk: ApkGlobal, i: Int): Unit = {
    val apkName = apk.projectName
    val nativeInfoMap: MMap[String, MMap[String, MMap[String, (String, String, IList[String])]]] = mmapEmpty
    val soFiles = nativeInfoMap.getOrElseUpdate(apkName, mmapEmpty)
    nativeMethodSoMap.foreach { case (sig, soName) =>
      bridge.getSoFilePath(apk.model.layout.outputSrcUri, soName) match {
        case Some(soPath) =>
          val jniFuncName = getJNIFunctionName(apk, sig)
          val javaFuncName = sig.methodName
          val jniSig = sig.proto
          println(soPath + " " + jniFuncName + " " + sig + " " + DataCommunicator.serializeParameters(sig))
          val funcs = soFiles.getOrElseUpdate(soPath, mmapEmpty)
//          funcs(jniFuncName) = (javaFuncName, jniSig, sig.getParameterTypes.map(_.jawaName))
        case None =>
      }
    }
    if (nativeInfoMap(apkName).nonEmpty) {
      DataCommunicator.serializeStatisticDatas(apk.model.layout.outputUri, i, nativeInfoMap)
    }
  }
}
