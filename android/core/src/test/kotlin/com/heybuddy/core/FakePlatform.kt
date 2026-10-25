package com.heybuddy.core

import com.heybuddy.core.tools.ToolError
import com.heybuddy.core.tools.ToolPlatform
import java.io.File

class FakePlatform(var answer: Boolean = true) : ToolPlatform {
    val asked = ArrayList<String>()
    val actions = ArrayList<String>()
    var clipboard = ""

    override suspend fun openUrl(url: String) = "opened $url".also { actions += it }
    override suspend fun openFile(file: File) = "opened ${file.name}".also { actions += it }
    override suspend fun share(text: String?, file: File?) = "shared".also { actions += "share:${file?.name ?: text}" }
    override suspend fun copyToClipboard(text: String): String { clipboard = text; return "copied ${text.length} characters" }
    override suspend fun readClipboard() = clipboard
    override suspend fun insertText(text: String) = "inserted ${text.length} characters".also { actions += "insert:$text" }
    override suspend fun launchApp(nameOrPackage: String): String {
        if (nameOrPackage == "Missing") throw ToolError("no app called Missing")
        actions += "launch:$nameOrPackage"; return "opened $nameOrPackage"
    }
    override suspend fun fetchUrl(url: String) = "text/html" to "<html><script>x()</script><p>Hello</p><p>World &amp; more</p></html>"
    override suspend fun approve(title: String, detail: String): Boolean { asked += title; return answer }
}
