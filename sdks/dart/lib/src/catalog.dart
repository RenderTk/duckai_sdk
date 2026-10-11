import 'dart:convert';
import 'package:flutter/services.dart';

class ModelInfo {
  final String id, name, provider, description, accessTier;
  final List<String> reasoningEfforts;
  final bool supportsImages, supportsPDF, supportsTools, beta;
  final AttachmentProfile? attachmentProfile;
  ModelInfo.fromJson(Map<String, dynamic> json)
    : id = json['id'] as String,
      name = json['name'] as String,
      provider = json['provider'] as String,
      description = json['description'] as String,
      accessTier = json['access_tier'] as String,
      reasoningEfforts = List.unmodifiable(
        (json['reasoning_efforts'] as List).cast<String>(),
      ),
      supportsImages = json['supports_images'] as bool,
      supportsPDF = json['supports_pdf'] as bool,
      supportsTools = json['supports_tools'] as bool,
      beta = json['beta'] as bool,
      attachmentProfile = json['attachment_profile'] == null
          ? null
          : AttachmentProfile.fromJson(
              json['attachment_profile'] as Map<String, dynamic>,
            );
  String get defaultReasoningEffort => reasoningEfforts.first;
}

class AttachmentProfile {
  final int maxDimension, imagesPerMessage, imagesPerConversation;
  final bool supportsPDF;
  AttachmentProfile.fromJson(Map<String, dynamic> json)
    : maxDimension = json['max_dimension'] as int,
      imagesPerMessage = json['images_per_message'] as int,
      imagesPerConversation = json['images_per_conversation'] as int,
      supportsPDF = json['supports_pdf'] as bool;
}

class Catalog {
  static Future<List<ModelInfo>>? _models;
  static Future<List<ModelInfo>> listModels({
    bool includeSubscriber = true,
  }) async {
    final models = await (_models ??= _load());
    return List.unmodifiable(
      models.where((m) => includeSubscriber || m.accessTier == 'free'),
    );
  }

  static Future<List<ModelInfo>> _load() async {
    try {
      final text = await rootBundle.loadString(
        'packages/duckai_sdk/assets/models.json',
      );
      return (jsonDecode(text) as List)
          .map((m) => ModelInfo.fromJson(m as Map<String, dynamic>))
          .toList();
    } catch (_) {
      _models = null;
      rethrow;
    }
  }

  static Future<ModelInfo?> getModel(String id) async {
    for (final m in await listModels()) {
      if (m.id == id) return m;
    }
    return null;
  }
}
