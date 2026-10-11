import Foundation
import ImageIO
import PDFKit
import UniformTypeIdentifiers
import ZIPFoundation

public struct Attachment: Sendable {
    public let data: Data
    public let filename: String
    public let mimeType: String?
    public init(data: Data, filename: String, mimeType: String? = nil) {
        self.data = data; self.filename = filename; self.mimeType = mimeType
    }

    /// Read a picker URL while its security-scoped grant is active.
    public static func fromURL(_ url: URL, maxBytes: Int = 20 * 1024 * 1024) throws -> Attachment {
        guard maxBytes > 0 else { throw AttachmentError(statusCode: 400, detail: "maxBytes must be positive") }
        let scoped = url.startAccessingSecurityScopedResource(); defer { if scoped { url.stopAccessingSecurityScopedResource() } }
        let handle = try FileHandle(forReadingFrom: url); defer { try? handle.close() }
        let bytes = try handle.read(upToCount: maxBytes + 1) ?? Data()
        guard bytes.count <= maxBytes else { throw AttachmentError(statusCode: 413, detail: "File exceeds upload limit") }
        return Attachment(data: bytes, filename: url.lastPathComponent)
    }
}

public struct AttachmentLimits: Sendable {
    public let model: String
    public let documentUploadBytes, documentMessageTextCharacters, maxImageUploadBytes, maxImagePixels: Int
    public let profile: AttachmentProfile?
    public let pdfBytesPerFileAndConversation = 5 * 1024 * 1024
    public let pdfPagesPerFile = 15
    public let pdfsPerConversation = 3
    public let imageMessageTextCharacters = 4500
    public let supportedFileExtensions = Documents.extensions.union(["pdf", "png", "jpg", "jpeg", "webp", "gif"]).sorted()
    init(model: String, settings: Settings) {
        self.model = model; profile = Catalog.getModel(model)?.attachmentProfile
        documentUploadBytes = settings.maxDocumentUploadBytes; documentMessageTextCharacters = settings.maxDocumentTextCharacters
        maxImageUploadBytes = settings.maxImageUploadBytes; maxImagePixels = settings.maxImagePixels
    }
}

enum Attachments {
    static func fail(_ message: String, _ status: Int = 422) throws -> Never {
        throw AttachmentError(statusCode: status, detail: message)
    }

    static func profile(_ model: String) throws -> AttachmentProfile {
        guard let p = Catalog.getModel(model)?.attachmentProfile else { try fail("\(model) has no known native attachment support; use a document text export") }; return p
    }

    static func checkPDF(_ bytes: Data) throws {
        if bytes.count > 5 * 1024 * 1024 { try fail("PDFs must fit within 5 MiB", 413) }
        guard bytes.starts(with: Data("%PDF-".utf8)), let pdf = PDFDocument(data: bytes) else { try fail("Invalid PDF") }
        guard !pdf.isEncrypted else { try fail("Upload an unencrypted PDF") }
        guard (1 ... 15).contains(pdf.pageCount) else { try fail("PDF must contain 1–15 pages") }
    }

    static func image(_ bytes: Data, settings: Settings, dimension: Int? = nil, mime: String? = nil) throws -> (Data, String) {
        guard bytes.count <= settings.maxImageUploadBytes else { try fail("Image exceeds upload byte limit", 413) }
        guard let source = CGImageSourceCreateWithData(bytes as CFData, nil), let type = CGImageSourceGetType(source) as String?,
              let properties = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any],
              let width = properties[kCGImagePropertyPixelWidth] as? Int, let height = properties[kCGImagePropertyPixelHeight] as? Int,
              width > 0, height > 0, width <= settings.maxImagePixels / height else { try fail("Invalid image or image exceeds pixel limit", 413) }
        let detected: String
        switch type { case "public.png": detected = "image/png"; case "public.jpeg": detected = "image/jpeg"; case "org.webmproject.webp": detected = "image/webp"; case "com.compuserve.gif": detected = "image/gif"; default: try fail("Supported images: PNG, JPEG, WebP and GIF", 415) }
        if let mime, mime != detected { try fail("Image contents do not match MIME type", 415) }
        guard let dimension else { return (bytes, detected) }
        let options: [CFString: Any] = [kCGImageSourceCreateThumbnailFromImageAlways: true, kCGImageSourceCreateThumbnailWithTransform: true, kCGImageSourceThumbnailMaxPixelSize: min(dimension, max(width, height))]
        guard let image = CGImageSourceCreateThumbnailAtIndex(source, 0, options as CFDictionary) else { try fail("Image could not be decoded") }
        let output = NSMutableData()
        guard let destination = CGImageDestinationCreateWithData(output, UTType.png.identifier as CFString, 1, nil) else { try fail("Image encoder unavailable") }
        CGImageDestinationAddImage(destination, image, nil)
        guard CGImageDestinationFinalize(destination) else { try fail("Image encoding failed") }
        return (output as Data, "image/png")
    }

    static func prepare(_ files: [Attachment], model: String, settings: Settings) throws -> [JSONValue] {
        var parts: [JSONValue] = []
        for file in files {
            try Task.checkCancellation()
            guard !file.data.isEmpty else { try fail("Files must not be empty") }
            let filename = file.filename.replacingOccurrences(of: "\\", with: "/").split(separator: "/").last.map(String.init)?.filter { $0.unicodeScalars.allSatisfy { !CharacterSet.controlCharacters.contains($0) } } ?? "attachment"
            let ext = (filename as NSString).pathExtension.lowercased()
            if Documents.extensions.contains(ext) {
                guard file.data.count <= settings.maxDocumentUploadBytes else { try fail("Document exceeds upload limit", 413) }
                let text = try Documents.extract(file.data, ext: ext)
                guard !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { try fail("Document has no readable text") }
                parts.append(.object(["type": .string("text"), "text": .string(Documents.prefix + filename + "]\n" + text)])); continue
            }
            let p = try profile(model)
            let mime = file.mimeType?.lowercased().split(separator: ";").first.map(String.init)
            if ext == "pdf" || mime == "application/pdf" || file.data.starts(with: Data("%PDF-".utf8)) {
                guard p.supportsPDF else { try fail("\(model) does not support PDF attachments") }
                try checkPDF(file.data)
                parts.append(.object(["type": .string("file"), "mimeType": .string("application/pdf"), "encoding": .string("base64"), "filename": .string(filename), "content": .string(file.data.base64EncodedString())]))
            } else {
                let (data, detected) = try image(file.data, settings: settings, dimension: p.maxDimension, mime: mime == "application/octet-stream" ? nil : mime)
                parts.append(.object(["type": .string("image"), "mimeType": .string(detected), "image": .string("data:\(detected);base64," + data.base64EncodedString())]))
            }
        }
        return parts.filter { $0["type"]?.string != "file" } + parts.filter { $0["type"]?.string == "file" }
    }

    static func validate(messages: [Message], model: String, settings: Settings) throws {
        var images = 0, pdfs = 0, pdfBytes = 0
        for message in messages where message.fields["role"]?.string == "user" {
            guard let parts = message.fields["content"]?.array else { continue }
            guard parts.allSatisfy({ $0.object != nil }) else { try fail("Content parts must be objects") }
            let texts = parts.filter { $0["type"]?.string == "text" }.compactMap { $0["text"]?.string }
            let characters = texts.joined().utf16.count
            if texts.contains(where: { $0.hasPrefix(Documents.prefix) }), characters > settings.maxDocumentTextCharacters { try fail("Prompt and documents exceed text limit", 413) }
            var count = 0
            for part in parts {
                if part["type"]?.string == "file" {
                    guard try profile(model).supportsPDF, part["mimeType"]?.string == "application/pdf", part["encoding"]?.string == "base64", let value = part["content"]?.string, value.count <= 6_990_508, let bytes = Data(base64Encoded: value) else { try fail("Invalid or unsupported PDF attachment") }
                    try checkPDF(bytes); pdfs += 1; pdfBytes += bytes.count
                } else if part["type"]?.string == "image" {
                    guard let mime = part["mimeType"]?.string, let value = part["image"]?.string, value.hasPrefix("data:\(mime);base64,"), value.count <= settings.maxImageUploadBytes * 4 / 3 + 256, let bytes = Data(base64Encoded: String(value.dropFirst("data:\(mime);base64,".count))) else { try fail("Invalid image data URL") }
                    _ = try image(bytes, settings: settings, mime: mime); count += 1
                }
            }
            if count > 0 {
                guard try count <= profile(model).imagesPerMessage else { try fail("Too many images per message") }
                if characters > 4500 { try fail("Messages with images support up to 4,500 text characters") }
            }
            images += count
        }
        if images > 0, try images > profile(model).imagesPerConversation { try fail("Too many images in conversation") }
        if pdfs > 3 { try fail("Too many PDFs in conversation") }
        if pdfBytes > 5 * 1024 * 1024 { try fail("Conversation PDFs exceed 5 MiB combined", 413) }
    }
}

private final class XMLNode {
    let name: String
    let attributes: [String: String]
    var children: [XMLNode] = []
    var value = ""
    init(_ name: String, _ attributes: [String: String]) {
        self.name = name.split(separator: ":").last.map(String.init) ?? name; self.attributes = attributes
    }

    func descendants(_ name: String) -> [XMLNode] {
        children.flatMap { ($0.name == name ? [$0] : []) + $0.descendants(name) }
    }

    func attr(_ name: String) -> String? {
        attributes.first { $0.key.split(separator: ":").last.map(String.init) == name }?.value
    }
}

private final class XMLText: NSObject, XMLParserDelegate {
    var root: XMLNode?
    var stack: [XMLNode] = []
    var failed = false
    var retainedCharacters = 0
    var nodes = 0
    func parser(_ parser: XMLParser, didStartElement elementName: String, namespaceURI _: String?, qualifiedName _: String?, attributes attributeDict: [String: String] = [:]) {
        nodes += 1
        if stack.count >= 128 || nodes > 100_000 { failed = true; parser.abortParsing(); return }
        let node = XMLNode(elementName, attributeDict)
        if let parent = stack.last { parent.children.append(node) } else { root = node }
        stack.append(node)
    }

    func parser(_ parser: XMLParser, foundCharacters string: String) {
        retainedCharacters += string.utf16.count * stack.count
        guard retainedCharacters <= 64 * 1024 * 1024 else { failed = true; parser.abortParsing(); return }
        for node in stack {
            node.value += string
        }
    }

    func parser(_: XMLParser, didEndElement _: String, namespaceURI _: String?, qualifiedName _: String?) {
        _ = stack.popLast()
    }

    func parser(_ parser: XMLParser, foundInternalEntityDeclarationWithName _: String, value _: String?) {
        failed = true; parser.abortParsing()
    }

    func parser(_ parser: XMLParser, foundExternalEntityDeclarationWithName _: String, publicID _: String?, systemID _: String?) {
        failed = true; parser.abortParsing()
    }
}

enum Documents {
    static let prefix = "[Attached document: "
    static let extensions: Set<String> = ["txt", "md", "csv", "tsv", "dif", "slk", "ics", "vcf", "json", "xml", "svg", "html", "htm", "xhtml", "docx", "docm", "dotx", "dotm", "xlsx", "xlsm", "xltx", "xltm", "xlam", "pptx", "pptm", "ppsx", "ppsm", "potx", "potm", "odt", "ott", "ods", "ots", "odp", "otp", "odg", "otg", "fodt", "fods", "fodp", "fodg", "vsdx", "vsdm", "vstx", "vstm", "vssx", "vssm", "vdx", "zip", "epub", "eml", "rtf", "mht", "mhtml"]
    private static func tree(_ data: Data) throws -> XMLNode {
        let probe = String(decoding: data, as: UTF8.self).lowercased()
        guard !probe.contains("<!doctype"), !probe.contains("<!entity") else { try Attachments.fail("XML entities/DTDs are not supported") }
        let parser = XMLParser(data: data); let delegate = XMLText(); parser.delegate = delegate; parser.shouldResolveExternalEntities = false
        guard parser.parse(), !delegate.failed, let root = delegate.root else { try Attachments.fail("Document XML is invalid") }
        return root
    }

    static func xml(_ data: Data) throws -> String {
        func walk(_ node: XMLNode) -> [String] {
            var output: [String] = []
            if ["page", "table", "sheet"].contains(node.name), let name = node.attr("name") { output.append(name) }
            if let value = node.attr("value") { output.append(value) }
            if ["p", "h", "text", "title", "desc", "t"].contains(node.name) || node.children.isEmpty {
                if !node.value.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { output.append(node.value) }
            } else { output += node.children.flatMap(walk) }
            return output
        }
        return try walk(tree(data)).joined(separator: "\n")
    }

    static func extract(_ data: Data, ext: String) throws -> String {
        let text = String(decoding: data, as: UTF8.self)
        if ["xml", "svg", "vdx", "fodt", "fods", "fodp", "fodg"].contains(ext) { return try xml(data) }
        if ["html", "htm", "xhtml"].contains(ext) {
            return text.replacingOccurrences(of: "(?is)<(script|style)[^>]*>.*?</\\1>", with: "", options: .regularExpression).replacingOccurrences(of: "<[^>]+>", with: "\n", options: .regularExpression).replacingOccurrences(of: "&amp;", with: "&").replacingOccurrences(of: "&lt;", with: "<").replacingOccurrences(of: "&gt;", with: ">")
        }
        if ext == "rtf" { return text.replacingOccurrences(of: "\\\\[a-zA-Z]+(?:-?\\d+)? ?|[{}]", with: "", options: .regularExpression) }
        let archived = ["docx", "docm", "dotx", "dotm", "xlsx", "xlsm", "xltx", "xltm", "xlam", "pptx", "pptm", "ppsx", "ppsm", "potx", "potm", "odt", "ott", "ods", "ots", "odp", "otp", "odg", "otg", "vsdx", "vsdm", "vstx", "vstm", "vssx", "vssm", "zip", "epub"].contains(ext)
        if !archived { return text }
        let archive: Archive
        do { archive = try Archive(data: data, accessMode: .read) } catch { try Attachments.fail("Document archive is invalid") }
        let entries = Array(archive)
        guard entries.count <= 2000 else { try Attachments.fail("Archive exceeds 2,000 members", 413) }
        var total: UInt64 = 0
        for entry in entries {
            guard entry.uncompressedSize <= 64 * 1024 * 1024 - total else { try Attachments.fail("Archive exceeds 64 MiB uncompressed", 413) }; total += entry.uncompressedSize
        }
        var chunks: [String] = []
        var files: [String: Data] = [:]
        for entry in entries where entry.type == .file && ["xml", "rels", "html", "htm", "xhtml", "txt", "md", "csv", "tsv", "opf"].contains((entry.path as NSString).pathExtension.lowercased()) {
            var bytes = Data(); _ = try archive.extract(entry) { bytes.append($0); if bytes.count > 64 * 1024 * 1024 { try Attachments.fail("Archive member too large", 413) } }; files[entry.path] = bytes
        }
        if ext.hasPrefix("xl") { return try sheets(files) }
        if ext.hasPrefix("ppt") || ext.hasPrefix("pps") || ext.hasPrefix("pot") { return try slides(files) }
        for entry in entries.sorted(by: { $0.path.localizedStandardCompare($1.path) == .orderedAscending }) {
            let path = entry.path.lowercased()
            let include: Bool = if ext.hasPrefix("doc") || ext.hasPrefix("dot") { path.hasPrefix("word/") && path.hasSuffix(".xml") && !path.contains("styles") && !path.contains("settings") && !path.contains("theme/") && !path.contains("fonttable") }
            else if ext.hasPrefix("xl") { path == "xl/workbook.xml" || path == "xl/sharedstrings.xml" || path.hasPrefix("xl/worksheets/") && path.hasSuffix(".xml") }
            else if ext.hasPrefix("ppt") || ext.hasPrefix("pps") || ext.hasPrefix("pot") { (path.hasPrefix("ppt/slides/slide") || path.hasPrefix("ppt/notesslides/notesslide")) && path.hasSuffix(".xml") }
            else if ext.hasPrefix("vs") { path.hasPrefix("visio/pages/") && path.hasSuffix(".xml") }
            else if ext == "epub" { path.hasSuffix(".html") || path.hasSuffix(".xhtml") }
            else if ext == "zip" { ["html", "htm", "txt", "md", "csv", "tsv"].contains((path as NSString).pathExtension) }
            else { path == "content.xml" }
            guard include, entry.type == .file else { continue }
            var bytes = Data()
            _ = try archive.extract(entry) { bytes.append($0); if bytes.count > 64 * 1024 * 1024 { try Attachments.fail("Archive member too large", 413) } }
            let memberExt = (path as NSString).pathExtension
            try chunks.append(entry.path + "\n" + (memberExt == "xml" ? xml(bytes) : extract(bytes, ext: memberExt)))
        }
        return chunks.joined(separator: "\n\n")
    }

    private static func sheets(_ files: [String: Data]) throws -> String {
        guard let wb = files["xl/workbook.xml"] else { try Attachments.fail("Workbook is missing") }
        let strings = try files["xl/sharedStrings.xml"].map { try tree($0).descendants("si").map { $0.descendants("t").map(\.value).joined() } } ?? []
        let rels = try files["xl/_rels/workbook.xml.rels"].map { try tree($0).descendants("Relationship") } ?? []
        var output: [String] = []
        for sheet in try tree(wb).descendants("sheet") {
            output.append("Sheet: " + (sheet.attr("name") ?? ""))
            guard let id = sheet.attr("id"), let target = rels.first(where: { $0.attr("Id") == id })?.attr("Target"), let data = files[target.hasPrefix("/") ? String(target.dropFirst()) : "xl/" + target] else { continue }
            for row in try tree(data).descendants("row") {
                var cells: [String] = []
                for cell in row.children where cell.name == "c" {
                    var value = cell.children.first(where: { $0.name == "v" })?.value ?? cell.descendants("t").map(\.value).joined()
                    if cell.attr("t") == "s", let i = Int(value), strings.indices.contains(i) { value = strings[i] }
                    cells.append(value)
                    if let f = cell.children.first(where: { $0.name == "f" }) { cells.append((cell.attr("r") ?? "") + ": =" + f.value) }
                }
                output.append("Row " + (row.attr("r") ?? "") + ": " + cells.joined(separator: " | "))
            }
        }
        return output.joined(separator: "\n")
    }

    private static func slides(_ files: [String: Data]) throws -> String {
        var paths: [String] = []
        if let pres = files["ppt/presentation.xml"], let data = files["ppt/_rels/presentation.xml.rels"] {
            let rels = try tree(data).descendants("Relationship")
            for slide in try tree(pres).descendants("sldId") {
                guard let id = slide.attributes.first(where: { $0.key.hasSuffix(":id") })?.value,
                      let target = rels.first(where: { $0.attr("Id") == id && $0.attr("TargetMode") != "External" })?.attr("Target") else { continue }
                paths.append(target.hasPrefix("/") ? String(target.dropFirst()) : "ppt/" + target)
            }
        }
        if paths.isEmpty { paths = files.keys.filter { $0.hasPrefix("ppt/slides/slide") && $0.hasSuffix(".xml") }.sorted { $0.localizedStandardCompare($1) == .orderedAscending } }
        var output: [String] = []
        for (i, path) in paths.enumerated() {
            guard let data = files[path] else { continue }; try output += ["Slide \(i + 1)", xml(data)]
            if let rel = files["ppt/slides/_rels/" + (path as NSString).lastPathComponent + ".rels"] {
                for r in try tree(rel).descendants("Relationship") where r.attr("TargetMode") != "External" {
                    guard let target = r.attr("Target"), target.contains("notesSlides/"), let notes = files[target.replacingOccurrences(of: "../", with: "ppt/")] else { continue }
                    try output.append(xml(notes))
                }
            }
        }
        return output.joined(separator: "\n")
    }
}
