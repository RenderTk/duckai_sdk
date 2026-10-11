using System.IO.Compression;
using System.Text;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;
using System.Xml;
using System.Xml.Linq;
using SkiaSharp;
using UglyToad.PdfPig;

namespace DuckAI;

public sealed record AttachmentLimits(string Model, int DocumentUploadBytes, int DocumentMessageTextCharacters, int MaxImageUploadBytes, int MaxImagePixels, AttachmentProfile? Profile, IReadOnlyList<string> SupportedFileExtensions)
{
    public int PdfBytesPerFileAndConversation => 5 * 1024 * 1024;
    public int PdfPagesPerFile => 15;
    public int PdfsPerConversation => 3;
    public int ImageMessageTextCharacters => 4500;
}
internal static class Attachments
{
    internal const string DocumentPrefix = "[Attached document: ";
    internal static readonly HashSet<string> Documents = new(["txt", "md", "csv", "tsv", "dif", "slk", "ics", "vcf", "json", "xml", "svg", "html", "htm", "xhtml", "docx", "docm", "dotx", "dotm", "xlsx", "xlsm", "xltx", "xltm", "xlam", "pptx", "pptm", "ppsx", "ppsm", "potx", "potm", "odt", "ott", "ods", "ots", "odp", "otp", "odg", "otg", "fodt", "fods", "fodp", "fodg", "vsdx", "vsdm", "vstx", "vstm", "vssx", "vssm", "vdx", "zip", "epub", "eml", "rtf", "mht", "mhtml"]);
    internal static AttachmentLimits Limits(string model, Settings settings) => new(model, settings.MaxDocumentUploadBytes, settings.MaxDocumentTextCharacters, settings.MaxImageUploadBytes, settings.MaxImagePixels, Catalog.GetModel(model)?.AttachmentProfile, Documents.Concat(["pdf", "png", "jpg", "jpeg", "webp", "gif"]).Order().ToArray());
    private static AttachmentProfile Profile(string model) => Catalog.GetModel(model)?.AttachmentProfile ?? throw new AttachmentException(422, model + " has no known native attachment support; use a text export");
    private static void Pdf(byte[] bytes)
    {
        if (bytes.Length > 5 * 1024 * 1024) throw new AttachmentException(413, "PDF exceeds 5 MiB");
        if (!bytes.AsSpan().StartsWith("%PDF-"u8)) throw new AttachmentException(422, "Invalid PDF");
        try { using var pdf = PdfDocument.Open(bytes); if (pdf.IsEncrypted) throw new AttachmentException(422, "Upload an unencrypted PDF"); if (pdf.NumberOfPages is < 1 or > 15) throw new AttachmentException(422, "PDF must contain 1–15 pages"); }
        catch (AttachmentException) { throw; }
        catch { throw new AttachmentException(422, "PDF is invalid or encrypted; upload an unencrypted export"); }
    }
    private static (byte[] Bytes, string Mime) Image(byte[] bytes, Settings settings, int? dimension = null, string? declared = null)
    {
        if (bytes.Length > settings.MaxImageUploadBytes) throw new AttachmentException(413, "Image exceeds upload limit");
        using var source = SKCodec.Create(new SKMemoryStream(bytes)) ?? throw new AttachmentException(422, "Invalid image");
        if (source.Info.Width <= 0 || source.Info.Height <= 0 || (long)source.Info.Width * source.Info.Height > settings.MaxImagePixels) throw new AttachmentException(413, "Image exceeds pixel limit");
        var mime = source.EncodedFormat switch { SKEncodedImageFormat.Png => "image/png", SKEncodedImageFormat.Jpeg => "image/jpeg", SKEncodedImageFormat.Webp => "image/webp", SKEncodedImageFormat.Gif => "image/gif", _ => throw new AttachmentException(415, "Supported images: PNG, JPEG, WebP and GIF") };
        if (declared is not null && declared != mime) throw new AttachmentException(415, "Image contents do not match MIME type");
        if (dimension is null) return (bytes, mime);
        using var bitmap = SKBitmap.Decode(bytes) ?? throw new AttachmentException(422, "Image could not be decoded");
        var origin = (int)source.EncodedOrigin;
        var swapped = origin is >= 5 and <= 8;
        using var oriented = new SKBitmap(swapped ? bitmap.Height : bitmap.Width, swapped ? bitmap.Width : bitmap.Height);
        using (var canvas = new SKCanvas(oriented))
        {
            var w = bitmap.Width; var h = bitmap.Height;
            var matrix = origin switch
            {
                2 => new SKMatrix(-1, 0, w, 0, 1, 0, 0, 0, 1),
                3 => new SKMatrix(-1, 0, w, 0, -1, h, 0, 0, 1),
                4 => new SKMatrix(1, 0, 0, 0, -1, h, 0, 0, 1),
                5 => new SKMatrix(0, 1, 0, 1, 0, 0, 0, 0, 1),
                6 => new SKMatrix(0, -1, h, 1, 0, 0, 0, 0, 1),
                7 => new SKMatrix(0, -1, h, -1, 0, w, 0, 0, 1),
                8 => new SKMatrix(0, 1, 0, -1, 0, w, 0, 0, 1),
                _ => SKMatrix.Identity
            };
            canvas.SetMatrix(matrix); canvas.DrawBitmap(bitmap, 0, 0);
        }
        var scale = Math.Min(1d, (double)dimension / Math.Max(oriented.Width, oriented.Height));
        using var resized = oriented.Resize(new SKImageInfo(Math.Max(1, (int)(oriented.Width * scale)), Math.Max(1, (int)(oriented.Height * scale))), new SKSamplingOptions(SKFilterMode.Linear)) ?? throw new AttachmentException(422, "Image resize failed");
        using var image = SKImage.FromBitmap(resized); using var encoded = image.Encode(SKEncodedImageFormat.Png, 100);
        return (encoded.ToArray(), "image/png");
    }
    internal static List<JsonObject> Prepare(IEnumerable<Attachment> files, string model, Settings settings)
    {
        var parts = new List<JsonObject>();
        foreach (var file in files)
        {
            if (file.Data.Length == 0) throw new AttachmentException(422, "Files must not be empty");
            var name = Regex.Replace(file.Filename.Replace('\\', '/').Split('/').Last(), "[\\x00-\\x1f\\x7f]", "");
            var ext = Path.GetExtension(name).TrimStart('.').ToLowerInvariant();
            if (Documents.Contains(ext)) { if (file.Data.Length > settings.MaxDocumentUploadBytes) throw new AttachmentException(413, "Document exceeds upload limit"); var text = Extract(file.Data, ext); if (string.IsNullOrWhiteSpace(text)) throw new AttachmentException(422, "Document has no readable text"); parts.Add(new() { ["type"] = "text", ["text"] = DocumentPrefix + name + "]\n" + text }); continue; }
            var p = Profile(model); var mime = file.MimeType?.Split(';')[0].Trim().ToLowerInvariant();
            if (ext == "pdf" || mime == "application/pdf" || file.Data.AsSpan().StartsWith("%PDF-"u8)) { if (!p.SupportsPdf) throw new AttachmentException(422, model + " does not support PDFs"); Pdf(file.Data); parts.Add(new() { ["type"] = "file", ["mimeType"] = "application/pdf", ["encoding"] = "base64", ["filename"] = name, ["content"] = Convert.ToBase64String(file.Data) }); }
            else { var (bytes, detected) = Image(file.Data, settings, p.MaxDimension, mime == "application/octet-stream" ? null : mime); parts.Add(new() { ["type"] = "image", ["mimeType"] = detected, ["image"] = "data:" + detected + ";base64," + Convert.ToBase64String(bytes) }); }
        }
        return parts.OrderBy(p => p["type"]?.ToString() == "file").ToList();
    }
    internal static void Validate(IEnumerable<JsonObject> messages, string model, Settings settings)
    {
        int images = 0, pdfs = 0, pdfBytes = 0;
        foreach (var m in messages.Where(m => m["role"]?.ToString() == "user" && m["content"] is JsonArray))
        {
            var parts = m["content"]!.AsArray(); if (parts.Any(p => p is not JsonObject)) throw new AttachmentException(422, "Content parts must be objects");
            var texts = parts.OfType<JsonObject>().Where(p => p["type"]?.ToString() == "text").Select(p => p["text"]?.GetValue<string>() ?? "").ToArray(); var length = string.Concat(texts).Length;
            if (texts.Any(t => t.StartsWith(DocumentPrefix)) && length > settings.MaxDocumentTextCharacters) throw new AttachmentException(413, "Prompt and documents exceed text limit");
            var count = 0;
            foreach (var p in parts.OfType<JsonObject>())
            {
                if (p["type"]?.ToString() == "file") { if (!Profile(model).SupportsPdf || p["mimeType"]?.ToString() != "application/pdf" || p["encoding"]?.ToString() != "base64") throw new AttachmentException(422, "Invalid or unsupported PDF attachment"); var bytes = Base64(p["content"]?.ToString() ?? "", 5 * 1024 * 1024); Pdf(bytes); pdfs++; pdfBytes += bytes.Length; }
                if (p["type"]?.ToString() == "image") { var mime = p["mimeType"]?.ToString(); var prefix = "data:" + mime + ";base64,"; var value = p["image"]?.ToString() ?? ""; if (!value.StartsWith(prefix)) throw new AttachmentException(422, "Invalid image data URL"); Image(Base64(value[prefix.Length..], settings.MaxImageUploadBytes), settings, declared: mime); count++; }
            }
            if (count > 0 && (count > Profile(model).ImagesPerMessage || length > 4500)) throw new AttachmentException(422, "Image count or 4,500-character message limit exceeded"); images += count;
        }
        if (images > 0 && images > Profile(model).ImagesPerConversation) throw new AttachmentException(422, "Too many images in conversation");
        if (pdfs > 3) throw new AttachmentException(422, "Too many PDFs in conversation"); if (pdfBytes > 5 * 1024 * 1024) throw new AttachmentException(413, "Conversation PDFs exceed 5 MiB combined");
    }
    private static byte[] Base64(string value, int limit) { if (value.Length > ((long)limit + 2) / 3 * 4 || value.Length % 4 != 0 || !Regex.IsMatch(value, "^[A-Za-z0-9+/]*={0,2}$")) throw new AttachmentException(422, "Invalid or oversized base64 attachment"); try { return Convert.FromBase64String(value); } catch { throw new AttachmentException(422, "Invalid base64 attachment"); } }
    private static XDocument Xml(byte[] data)
    {
        try
        {
            using var input = new MemoryStream(data); using var reader = XmlReader.Create(input, new() { DtdProcessing = DtdProcessing.Prohibit, XmlResolver = null, MaxCharactersInDocument = 64 * 1024 * 1024 }); var count = 0;
            while (reader.Read()) if (reader.Depth >= 128 || ++count > 100000) throw new AttachmentException(413, "Document XML is too complex");
            input.Position = 0;
            using var safe = XmlReader.Create(input, new() { DtdProcessing = DtdProcessing.Prohibit, XmlResolver = null, MaxCharactersInDocument = 64 * 1024 * 1024 });
            return XDocument.Load(safe);
        }
        catch (AttachmentException) { throw; }
        catch { throw new AttachmentException(422, "Invalid document XML; entities/DTDs are not supported"); }
    }
    private static string TextXml(byte[] bytes)
    {
        IEnumerable<string> Walk(XElement e)
        {
            var label = e.Attributes().FirstOrDefault(a => a.Name.LocalName == "name")?.Value;
            if (label is not null && e.Name.LocalName is "table" or "page" or "sheet") yield return label;
            var number = e.Attributes().FirstOrDefault(a => a.Name.LocalName == "value")?.Value;
            if (number is not null) yield return number;
            if (e.Name.LocalName is "p" or "h" or "text" or "title" or "desc" or "t" || !e.HasElements) { if (!string.IsNullOrWhiteSpace(e.Value)) yield return e.Value; }
            else foreach (var child in e.Elements()) foreach (var value in Walk(child)) yield return value;
        }
        var doc = Xml(bytes); return doc.Root is null ? "" : string.Join("\n", Walk(doc.Root));
    }
    private static string Html(string text) => System.Net.WebUtility.HtmlDecode(Regex.Replace(Regex.Replace(text, "<(script|style)[^>]*>.*?</\\1>", "", RegexOptions.IgnoreCase | RegexOptions.Singleline), "<[^>]+>", "\n"));
    private static string Extract(byte[] data, string ext)
    {
        var text = Encoding.UTF8.GetString(data);
        if (new[] { "xml", "svg", "vdx", "fodt", "fods", "fodp", "fodg" }.Contains(ext)) return TextXml(data);
        if (new[] { "html", "htm", "xhtml" }.Contains(ext)) return Html(text);
        if (ext == "rtf") return Regex.Replace(Regex.Replace(text, @"\\(?:[a-zA-Z]+)(?:-?\d+)? ?|[{}]", ""), @"\\'([0-9a-fA-F]{2})", m => ((char)Convert.ToByte(m.Groups[1].Value, 16)).ToString());
        var archived = new[] { "docx", "docm", "dotx", "dotm", "xlsx", "xlsm", "xltx", "xltm", "xlam", "pptx", "pptm", "ppsx", "ppsm", "potx", "potm", "odt", "ott", "ods", "ots", "odp", "otp", "odg", "otg", "vsdx", "vsdm", "vstx", "vstm", "vssx", "vssm", "zip", "epub" }.Contains(ext);
        if (!archived) return text;
        try
        {
            using var archive = new ZipArchive(new MemoryStream(data), ZipArchiveMode.Read);
            if (archive.Entries.Count > 2000 || archive.Entries.Sum(e => e.Length) > 64 * 1024 * 1024) throw new AttachmentException(413, "Archive exceeds 2,000 members or 64 MiB uncompressed");
            var members = new Dictionary<string, byte[]>();
            foreach (var entry in archive.Entries) { if (entry.Length == 0 || !new[] { ".xml", ".rels", ".html", ".htm", ".xhtml", ".txt", ".md", ".csv", ".tsv", ".opf" }.Contains(Path.GetExtension(entry.FullName).ToLowerInvariant())) continue; using var stream = entry.Open(); using var output = new MemoryStream(); stream.CopyTo(output); members[entry.FullName] = output.ToArray(); }
            if (ext.StartsWith("xl")) return SheetText(members);
            if (ext.StartsWith("ppt") || ext.StartsWith("pps") || ext.StartsWith("pot")) return SlidesText(members);
            var selected = members.Where(m => ext.StartsWith("doc") || ext.StartsWith("dot") ? m.Key.StartsWith("word/") && !m.Key.Contains("styles") && !m.Key.Contains("settings") && !m.Key.Contains("theme/") && !m.Key.Contains("fontTable") : ext.StartsWith("vs") ? m.Key.StartsWith("visio/pages/") : ext == "zip" || ext == "epub" ? !m.Key.EndsWith(".xml") && !m.Key.EndsWith(".rels") && !m.Key.EndsWith(".opf") : m.Key == "content.xml");
            return string.Join("\n\n", selected.OrderBy(m => m.Key).Select(m => m.Key + "\n" + (m.Key.EndsWith(".xml") ? TextXml(m.Value) : Extract(m.Value, Path.GetExtension(m.Key).TrimStart('.')))));
        }
        catch (AttachmentException) { throw; }
        catch { throw new AttachmentException(422, "Document archive is invalid or encrypted"); }
    }
    private static string SheetText(Dictionary<string, byte[]> files)
    {
        var output = new StringBuilder(); var strings = files.TryGetValue("xl/sharedStrings.xml", out var ss) ? Xml(ss).Descendants().Where(e => e.Name.LocalName == "si").Select(e => string.Concat(e.Descendants().Where(t => t.Name.LocalName == "t").Select(t => t.Value))).ToArray() : [];
        if (!files.TryGetValue("xl/workbook.xml", out var wb)) throw new AttachmentException(422, "Workbook is missing");
        var rels = files.TryGetValue("xl/_rels/workbook.xml.rels", out var r) ? Xml(r).Descendants().Where(e => e.Name.LocalName == "Relationship").ToDictionary(e => e.Attribute("Id")!.Value, e => e.Attribute("Target")!.Value) : new Dictionary<string, string>();
        foreach (var sheet in Xml(wb).Descendants().Where(e => e.Name.LocalName == "sheet"))
        {
            var id = sheet.Attributes().FirstOrDefault(a => a.Name.LocalName == "id")?.Value;
            output.AppendLine("Sheet: " + sheet.Attribute("name")?.Value);
            if (id is null || !rels.TryGetValue(id, out var target)) continue; var path = target.StartsWith('/') ? target.TrimStart('/') : "xl/" + target;
            if (!files.TryGetValue(path, out var bytes)) continue;
            foreach (var row in Xml(bytes).Descendants().Where(e => e.Name.LocalName == "row"))
            {
                var cells = new List<string>();
                foreach (var cell in row.Elements().Where(e => e.Name.LocalName == "c"))
                {
                    var value = cell.Elements().FirstOrDefault(e => e.Name.LocalName == "v")?.Value ?? string.Concat(cell.Descendants().Where(e => e.Name.LocalName == "t").Select(e => e.Value));
                    if (cell.Attribute("t")?.Value == "s" && int.TryParse(value, out var i) && i >= 0 && i < strings.Length) value = strings[i];
                    cells.Add(value); if (cell.Elements().FirstOrDefault(e => e.Name.LocalName == "f") is { } formula) cells.Add(cell.Attribute("r")?.Value + ": =" + formula.Value);
                }
                output.AppendLine("Row " + row.Attribute("r")?.Value + ": " + string.Join(" | ", cells));
            }
        }
        return output.ToString();
    }
    private static string SlidesText(Dictionary<string, byte[]> files)
    {
        var output = new StringBuilder(); var paths = new List<string>();
        if (files.TryGetValue("ppt/presentation.xml", out var pres) && files.TryGetValue("ppt/_rels/presentation.xml.rels", out var rel))
        {
            var rels = Xml(rel).Descendants().Where(e => e.Name.LocalName == "Relationship").ToDictionary(e => e.Attribute("Id")!.Value, e => e.Attribute("Target")!.Value);
            foreach (var slide in Xml(pres).Descendants().Where(e => e.Name.LocalName == "sldId")) { var id = slide.Attributes().FirstOrDefault(a => a.Name.LocalName == "id" && a.Name.NamespaceName.Length > 0)?.Value; if (id is not null && rels.TryGetValue(id, out var target)) paths.Add(target.StartsWith('/') ? target.TrimStart('/') : "ppt/" + target); }
        }
        if (paths.Count == 0) paths = files.Keys.Where(p => Regex.IsMatch(p, @"^ppt/slides/slide\d+\.xml$")).OrderBy(p => int.Parse(Regex.Match(p, @"\d+").Value)).ToList();
        for (var i = 0; i < paths.Count; i++) { var path = paths[i]; if (!files.TryGetValue(path, out var bytes)) continue; output.AppendLine($"Slide {i + 1}"); output.AppendLine(TextXml(bytes)); var relPath = "ppt/slides/_rels/" + Path.GetFileName(path) + ".rels"; if (files.TryGetValue(relPath, out var slideRel)) foreach (var r in Xml(slideRel).Descendants().Where(e => e.Attribute("TargetMode")?.Value != "External" && (e.Attribute("Type")?.Value.EndsWith("/notesSlide") == true || e.Attribute("Target")?.Value.Contains("notesSlides/") == true))) { var target = r.Attribute("Target")?.Value; var notes = target?.Replace("../", "ppt/"); if (notes is not null && files.TryGetValue(notes, out var n)) output.AppendLine(TextXml(n)); } }
        return output.ToString();
    }
}
