import 'dart:convert';
import 'dart:typed_data';
import 'package:archive/archive.dart';
import 'package:html/parser.dart' as html;
import 'package:image/image.dart' as image;
import 'package:pdfx/pdfx.dart';
import 'package:xml/xml.dart';
import 'catalog.dart';
import 'types.dart';

const documentPrefix = '[Attached document: ';
const _documents = {
  'txt',
  'md',
  'csv',
  'tsv',
  'dif',
  'slk',
  'ics',
  'vcf',
  'json',
  'xml',
  'svg',
  'html',
  'htm',
  'xhtml',
  'docx',
  'docm',
  'dotx',
  'dotm',
  'xlsx',
  'xlsm',
  'xltx',
  'xltm',
  'xlam',
  'pptx',
  'pptm',
  'ppsx',
  'ppsm',
  'potx',
  'potm',
  'odt',
  'ott',
  'ods',
  'ots',
  'odp',
  'otp',
  'odg',
  'otg',
  'fodt',
  'fods',
  'fodp',
  'fodg',
  'vsdx',
  'vsdm',
  'vstx',
  'vstm',
  'vssx',
  'vssm',
  'vdx',
  'zip',
  'epub',
  'eml',
  'rtf',
  'mht',
  'mhtml',
};
List<String> supportedFileExtensions() =>
    [..._documents, 'pdf', 'png', 'jpg', 'jpeg', 'webp', 'gif']..sort();

class AttachmentLimits {
  final String model;
  final Settings settings;
  final AttachmentProfile? profile;
  AttachmentLimits(this.model, this.settings, this.profile);
  int get documentUploadBytes => settings.maxDocumentUploadBytes;
  int get documentMessageTextCharacters => settings.maxDocumentTextCharacters;
  int get maxImageUploadBytes => settings.maxImageUploadBytes;
  int get maxImagePixels => settings.maxImagePixels;
  int get pdfBytesPerFileAndConversation => 5 * 1024 * 1024;
  int get pdfPagesPerFile => 15;
  int get pdfsPerConversation => 3;
  int get imageMessageTextCharacters => 4500;
  List<String> get fileExtensions => supportedFileExtensions();
}

class Attachments {
  static Future<AttachmentLimits> limits(
    String model,
    Settings settings,
  ) async => AttachmentLimits(
    model,
    settings,
    (await Catalog.getModel(model))?.attachmentProfile,
  );
  static Future<AttachmentProfile> _profile(String model) async =>
      (await Catalog.getModel(model))?.attachmentProfile ??
      (throw AttachmentException(
        422,
        '$model has no known native attachment support; use a text export',
      ));
  static Future<void> _pdf(Uint8List bytes) async {
    if (bytes.length > 5 * 1024 * 1024) {
      throw AttachmentException(413, 'PDF exceeds 5 MiB');
    }
    if (bytes.length < 5 ||
        ascii.decode(bytes.sublist(0, 5), allowInvalid: true) != '%PDF-') {
      throw AttachmentException(422, 'Invalid PDF');
    }
    PdfDocument? document;
    try {
      document = await PdfDocument.openData(bytes);
      if (document.pagesCount < 1 || document.pagesCount > 15) {
        throw AttachmentException(422, 'PDF must contain 1–15 pages');
      }
    } catch (e) {
      if (e is AttachmentException) rethrow;
      throw AttachmentException(
        422,
        'PDF is invalid or encrypted; upload an unencrypted export',
      );
    } finally {
      await document?.close();
    }
  }

  static (Uint8List, String) _image(
    Uint8List bytes,
    Settings settings, {
    int? dimension,
    String? declared,
  }) {
    if (bytes.length > settings.maxImageUploadBytes) {
      throw AttachmentException(413, 'Image exceeds upload limit');
    }
    final decoder = image.findDecoderForData(bytes);
    final info = decoder?.startDecode(bytes);
    if (info == null || info.width <= 0 || info.height <= 0) {
      throw AttachmentException(422, 'Invalid image');
    }
    if (info.width * info.height > settings.maxImagePixels) {
      throw AttachmentException(413, 'Image exceeds pixel limit');
    }
    final mime = decoder is image.PngDecoder
        ? 'image/png'
        : decoder is image.JpegDecoder
        ? 'image/jpeg'
        : decoder is image.WebPDecoder
        ? 'image/webp'
        : decoder is image.GifDecoder
        ? 'image/gif'
        : throw AttachmentException(
            415,
            'Supported images: PNG, JPEG, WebP and GIF',
          );
    if (declared != null && declared != mime) {
      throw AttachmentException(415, 'Image contents do not match MIME type');
    }
    if (dimension == null) return (bytes, mime);
    final decoded = decoder!.decodeFrame(0);
    if (decoded == null) {
      throw AttachmentException(422, 'Image could not be decoded');
    }
    var result = image.bakeOrientation(decoded);
    if (result.width > dimension || result.height > dimension) {
      result = image.copyResize(
        result,
        width: result.width >= result.height ? dimension : null,
        height: result.height > result.width ? dimension : null,
        interpolation: image.Interpolation.linear,
      );
    }
    return (image.encodePng(result), 'image/png');
  }

  static Future<List<ContentPart>> prepare(
    List<Attachment> files,
    String model,
    Settings settings, {
    CancellationToken? cancellationToken,
  }) async {
    final parts = <ContentPart>[];
    for (final file in files) {
      cancellationToken?.throwIfCancelled();
      if (file.data.isEmpty) {
        throw AttachmentException(422, 'Files must not be empty');
      }
      final name = file.filename
          .replaceAll('\\', '/')
          .split('/')
          .last
          .replaceAll(RegExp(r'[\x00-\x1f\x7f]'), '');
      final ext = name.split('.').last.toLowerCase();
      if (_documents.contains(ext)) {
        if (file.data.length > settings.maxDocumentUploadBytes) {
          throw AttachmentException(413, 'Document exceeds upload limit');
        }
        final text = _extract(file.data, ext);
        if (text.trim().isEmpty) {
          throw AttachmentException(422, 'Document has no readable text');
        }
        parts.add({'type': 'text', 'text': '$documentPrefix$name]\n$text'});
        continue;
      }
      final p = await _profile(model);
      final mime = file.mimeType?.toLowerCase().split(';').first.trim();
      if (ext == 'pdf' ||
          mime == 'application/pdf' ||
          (file.data.length >= 5 &&
              ascii.decode(file.data.sublist(0, 5), allowInvalid: true) ==
                  '%PDF-')) {
        if (!p.supportsPDF) {
          throw AttachmentException(422, '$model does not support PDFs');
        }
        await _pdf(file.data);
        parts.add({
          'type': 'file',
          'mimeType': 'application/pdf',
          'encoding': 'base64',
          'filename': name,
          'content': base64Encode(file.data),
        });
      } else {
        final (bytes, detected) = _image(
          file.data,
          settings,
          dimension: p.maxDimension,
          declared: mime == 'application/octet-stream' ? null : mime,
        );
        parts.add({
          'type': 'image',
          'mimeType': detected,
          'image': 'data:$detected;base64,${base64Encode(bytes)}',
        });
      }
    }
    return [
      ...parts.where((p) => p['type'] != 'file'),
      ...parts.where((p) => p['type'] == 'file'),
    ];
  }

  static Uint8List _base64(String value, int limit) {
    if (value.length > ((limit + 2) ~/ 3) * 4 ||
        value.length % 4 != 0 ||
        !RegExp(r'^[A-Za-z0-9+/]*={0,2}$').hasMatch(value)) {
      throw AttachmentException(422, 'Invalid or oversized base64');
    }
    try {
      return base64Decode(value);
    } catch (_) {
      throw AttachmentException(422, 'Invalid base64');
    }
  }

  static Future<void> validate(
    List<Message> messages,
    String model,
    Settings settings,
  ) async {
    var images = 0, pdfs = 0, pdfBytes = 0;
    for (final message in messages.where(
      (m) => m['role'] == 'user' && m['content'] is List,
    )) {
      final parts = message['content'] as List;
      if (parts.any((p) => p is! Map)) {
        throw AttachmentException(422, 'Content parts must be objects');
      }
      final texts = parts
          .where((p) => p['type'] == 'text')
          .map((p) => p['text'] as String? ?? '')
          .toList();
      final length = texts.join().length;
      if (texts.any((t) => t.startsWith(documentPrefix)) &&
          length > settings.maxDocumentTextCharacters) {
        throw AttachmentException(
          413,
          'Prompt and documents exceed text limit',
        );
      }
      var count = 0;
      for (final part in parts) {
        if (part['type'] == 'file') {
          if (!(await _profile(model)).supportsPDF ||
              part['mimeType'] != 'application/pdf' ||
              part['encoding'] != 'base64') {
            throw AttachmentException(422, 'Invalid or unsupported PDF');
          }
          final bytes = _base64(
            part['content'] as String? ?? '',
            5 * 1024 * 1024,
          );
          await _pdf(bytes);
          pdfs++;
          pdfBytes += bytes.length;
        } else if (part['type'] == 'image') {
          final mime = part['mimeType'] as String?;
          final prefix = 'data:$mime;base64,';
          final value = part['image'] as String? ?? '';
          if (!value.startsWith(prefix)) {
            throw AttachmentException(422, 'Invalid image data URL');
          }
          _image(
            _base64(
              value.substring(prefix.length),
              settings.maxImageUploadBytes,
            ),
            settings,
            declared: mime,
          );
          count++;
        }
      }
      if (count > 0 &&
          (count > (await _profile(model)).imagesPerMessage || length > 4500)) {
        throw AttachmentException(
          422,
          'Image count or 4,500-character message limit exceeded',
        );
      }
      images += count;
    }
    if (images > 0 && images > (await _profile(model)).imagesPerConversation) {
      throw AttachmentException(422, 'Too many images in conversation');
    }
    if (pdfs > 3) {
      throw AttachmentException(422, 'Too many PDFs in conversation');
    }
    if (pdfBytes > 5 * 1024 * 1024) {
      throw AttachmentException(413, 'Conversation PDFs exceed 5 MiB combined');
    }
  }

  static XmlDocument _xml(List<int> bytes) {
    final text = utf8.decode(bytes, allowMalformed: true);
    if (RegExp(r'<!\s*(DOCTYPE|ENTITY)', caseSensitive: false).hasMatch(text)) {
      throw AttachmentException(422, 'XML entities/DTDs are not supported');
    }
    try {
      final document = XmlDocument.parse(text);
      final pending = <(XmlNode, int)>[(document.rootElement, 0)];
      var count = 0;
      while (pending.isNotEmpty) {
        final (node, depth) = pending.removeLast();
        if (++count > 100000 || depth >= 128) {
          throw AttachmentException(413, 'Document XML is too complex');
        }
        pending.addAll(node.children.map((child) => (child, depth + 1)));
      }
      return document;
    } catch (_) {
      throw AttachmentException(422, 'Invalid document XML');
    }
  }

  static Iterable<XmlElement> _elements(XmlNode node, String name) => node
      .descendants
      .whereType<XmlElement>()
      .where((e) => e.name.local == name);
  static String _textXML(List<int> bytes) {
    final output = <String>[];
    void walk(XmlElement element) {
      final label = element.attributes
          .where((a) => a.name.local == 'name')
          .firstOrNull
          ?.value;
      if (label != null &&
          {'page', 'table', 'sheet'}.contains(element.name.local)) {
        output.add(label);
      }
      final number = element.attributes
          .where((a) => a.name.local == 'value')
          .firstOrNull
          ?.value;
      if (number != null) output.add(number);
      if ({
            'p',
            'h',
            'text',
            'title',
            'desc',
            't',
          }.contains(element.name.local) ||
          element.childElements.isEmpty) {
        if (element.innerText.trim().isNotEmpty) output.add(element.innerText);
      } else {
        for (final child in element.childElements) {
          walk(child);
        }
      }
    }

    walk(_xml(bytes).rootElement);
    return output.join('\n');
  }

  static String _extract(List<int> data, String ext) {
    final text = utf8.decode(data, allowMalformed: true);
    if ({'xml', 'svg', 'vdx', 'fodt', 'fods', 'fodp', 'fodg'}.contains(ext)) {
      return _textXML(data);
    }
    if ({'html', 'htm', 'xhtml'}.contains(ext)) {
      final doc = html.parse(text);
      for (final n in doc.querySelectorAll('script,style')) {
        n.remove();
      }
      return doc.body?.text ?? '';
    }
    if (ext == 'rtf') {
      return text
          .replaceAll(RegExp(r'\\[a-zA-Z]+(?:-?\d+)? ?|[{}]'), '')
          .replaceAllMapped(
            RegExp(r"\\'([0-9a-fA-F]{2})"),
            (m) => String.fromCharCode(int.parse(m[1]!, radix: 16)),
          );
    }
    const archives = {
      'docx',
      'docm',
      'dotx',
      'dotm',
      'xlsx',
      'xlsm',
      'xltx',
      'xltm',
      'xlam',
      'pptx',
      'pptm',
      'ppsx',
      'ppsm',
      'potx',
      'potm',
      'odt',
      'ott',
      'ods',
      'ots',
      'odp',
      'otp',
      'odg',
      'otg',
      'vsdx',
      'vsdm',
      'vstx',
      'vstm',
      'vssx',
      'vssm',
      'zip',
      'epub',
    };
    if (!archives.contains(ext)) return text;
    try {
      final archive = ZipDecoder().decodeBytes(data, verify: true);
      if (archive.length > 2000 ||
          archive.fold<int>(0, (n, e) => n + e.size) > 64 * 1024 * 1024) {
        throw AttachmentException(
          413,
          'Archive exceeds 2,000 members or 64 MiB uncompressed',
        );
      }
      final files = <String, List<int>>{};
      for (final file in archive) {
        if (file.isFile &&
            {
              'xml',
              'rels',
              'html',
              'htm',
              'xhtml',
              'txt',
              'md',
              'csv',
              'tsv',
              'opf',
            }.contains(file.name.split('.').last.toLowerCase())) {
          files[file.name] = file.content;
        }
      }
      if (ext.startsWith('xl')) return _sheets(files);
      if (ext.startsWith('ppt') ||
          ext.startsWith('pps') ||
          ext.startsWith('pot')) {
        return _slides(files);
      }
      final selected =
          files.keys
              .where(
                (path) => ext.startsWith('doc') || ext.startsWith('dot')
                    ? path.startsWith('word/') &&
                          !path.contains('styles') &&
                          !path.contains('settings') &&
                          !path.contains('theme/') &&
                          !path.contains('fontTable')
                    : ext.startsWith('vs')
                    ? path.startsWith('visio/pages/')
                    : ext == 'zip' || ext == 'epub'
                    ? !{'xml', 'rels', 'opf'}.contains(path.split('.').last)
                    : path == 'content.xml',
              )
              .toList()
            ..sort();
      return selected
          .map(
            (path) =>
                '$path\n${path.endsWith('.xml') ? _textXML(files[path]!) : _extract(files[path]!, path.split('.').last)}',
          )
          .join('\n\n');
    } catch (e) {
      if (e is AttachmentException) rethrow;
      throw AttachmentException(
        422,
        'Document archive is invalid or encrypted',
      );
    }
  }

  static String _sheets(Map<String, List<int>> files) {
    final output = StringBuffer();
    final ss = files['xl/sharedStrings.xml'];
    final strings = ss == null
        ? <String>[]
        : _elements(_xml(ss), 'si')
              .map((e) => _elements(e, 't').map((t) => t.innerText).join())
              .toList();
    final wb = files['xl/workbook.xml'];
    if (wb == null) throw AttachmentException(422, 'Workbook is missing');
    final rel = files['xl/_rels/workbook.xml.rels'];
    final rels = rel == null
        ? <String, String>{}
        : {
            for (final e in _elements(_xml(rel), 'Relationship'))
              e.getAttribute('Id')!: e.getAttribute('Target')!,
          };
    for (final sheet in _elements(_xml(wb), 'sheet')) {
      output.writeln('Sheet: ${sheet.getAttribute('name')}');
      final id = sheet.attributes
          .where((a) => a.name.local == 'id')
          .firstOrNull
          ?.value;
      final target = rels[id];
      if (target == null) continue;
      final path = target.startsWith('/') ? target.substring(1) : 'xl/$target';
      final bytes = files[path];
      if (bytes == null) continue;
      for (final row in _elements(_xml(bytes), 'row')) {
        final cells = <String>[];
        for (final cell in row.childElements.where(
          (e) => e.name.local == 'c',
        )) {
          var value =
              cell.childElements
                  .where((e) => e.name.local == 'v')
                  .firstOrNull
                  ?.innerText ??
              _elements(cell, 't').map((e) => e.innerText).join();
          if (cell.getAttribute('t') == 's') {
            final index = int.tryParse(value);
            if (index != null && index >= 0 && index < strings.length) {
              value = strings[index];
            }
          }
          cells.add(value);
          final formula = cell.childElements
              .where((e) => e.name.local == 'f')
              .firstOrNull;
          if (formula != null) {
            cells.add('${cell.getAttribute('r')}: =${formula.innerText}');
          }
        }
        output.writeln('Row ${row.getAttribute('r')}: ${cells.join(' | ')}');
      }
    }
    return output.toString();
  }

  static String _slides(Map<String, List<int>> files) {
    final output = StringBuffer();
    final paths = <String>[];
    final pres = files['ppt/presentation.xml'],
        rel = files['ppt/_rels/presentation.xml.rels'];
    if (pres != null && rel != null) {
      final rels = {
        for (final e in _elements(_xml(rel), 'Relationship'))
          e.getAttribute('Id')!: e.getAttribute('Target')!,
      };
      for (final slide in _elements(_xml(pres), 'sldId')) {
        final id = slide.attributes
            .where((a) => a.name.local == 'id' && a.name.prefix != null)
            .firstOrNull
            ?.value;
        final target = rels[id];
        if (target != null) {
          paths.add(
            target.startsWith('/') ? target.substring(1) : 'ppt/$target',
          );
        }
      }
    }
    if (paths.isEmpty) {
      paths.addAll(
        files.keys.where(
          (p) => RegExp(r'^ppt/slides/slide\d+\.xml$').hasMatch(p),
        ),
      );
      paths.sort(
        (a, b) => int.parse(
          RegExp(r'\d+').firstMatch(a)![0]!,
        ).compareTo(int.parse(RegExp(r'\d+').firstMatch(b)![0]!)),
      );
    }
    for (var i = 0; i < paths.length; i++) {
      final path = paths[i], bytes = files[paths[i]];
      if (bytes == null) continue;
      output.writeln('Slide ${i + 1}');
      output.writeln(_textXML(bytes));
      final r = files['ppt/slides/_rels/${path.split('/').last}.rels'];
      if (r != null) {
        for (final e in _elements(_xml(r), 'Relationship').where(
          (e) =>
              e.getAttribute('TargetMode') != 'External' &&
              (e.getAttribute('Type')?.endsWith('/notesSlide') == true ||
                  e.getAttribute('Target')?.contains('notesSlides/') == true),
        )) {
          final target = e.getAttribute('Target')?.replaceFirst('../', 'ppt/');
          final notes = files[target];
          if (notes != null) output.writeln(_textXML(notes));
        }
      }
    }
    return output.toString();
  }
}
