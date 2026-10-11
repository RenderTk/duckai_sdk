import 'dart:async';
import 'dart:convert';
import 'package:flutter/services.dart';
import 'package:flutter/material.dart';
import 'package:duckai_sdk/duckai_sdk.dart';

void main() {
  WidgetsFlutterBinding.ensureInitialized();
  runApp(const MaterialApp(home: ChatPage()));
}

class ChatPage extends StatefulWidget {
  const ChatPage({super.key});
  @override
  State<ChatPage> createState() => _ChatPageState();
}

class _ChatPageState extends State<ChatPage> with WidgetsBindingObserver {
  final prompt = TextEditingController(
    text: 'Explain recursion with a short example.',
  );
  String output = 'Ready';
  CancellationToken? cancellation;
  DuckAI? client;
  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    if (const bool.fromEnvironment('DUCKAI_LIVE_TEST')) {
      WidgetsBinding.instance.addPostFrameCallback((_) {
        prompt.text = 'Reply with: native mobile session works';
        send();
      });
    }
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.paused) cancellation?.cancel();
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    cancellation?.cancel();
    unawaited(client?.close() ?? Future.value());
    prompt.dispose();
    super.dispose();
  }

  Future<void> send() async {
    final token = CancellationToken();
    final ai = DuckAI(model: Model.mistralSmall4);
    client = ai;
    setState(() {
      output = '';
      cancellation = token;
    });
    try {
      if (const bool.fromEnvironment('DUCKAI_LIVE_TEST')) {
        final fixtures =
            jsonDecode(await rootBundle.loadString('assets/smoke-files.json'))
                as List;
        final parts = await ai.prepareFiles([
          for (final f in fixtures)
            Attachment(
              base64Decode(f['data'] as String),
              f['filename'] as String,
            ),
        ], model: Model.gpt6Luna);
        if (!parts.any((p) => p['type'] == 'image') ||
            !parts.any((p) => p['type'] == 'file')) {
          throw StateError('Native image/PDF preparation failed');
        }
        debugPrint('NATIVE ATTACHMENT CHECK PASSED');
      }
      final chat = ai.conversation();
      await for (final delta in chat.stream(
        prompt.text,
        options: ChatOptions(cancellationToken: token),
      )) {
        if (mounted) {
          setState(() {
            output += delta;
          });
        }
      }
      if (const bool.fromEnvironment('DUCKAI_LIVE_TEST')) {
        final second = await chat.chat(
          'What were the last three words in your previous answer?',
          options: ChatOptions(cancellationToken: token),
        );
        if (!second.done || second.text.isEmpty || chat.messages.length != 4) {
          throw StateError('Conversation did not complete');
        }
        if (mounted) {
          setState(() {
            output +=
                '\nFollow-up: ${second.text}\nLIVE CHECK PASSED: ${chat.messages.length} history messages';
          });
        }
        debugPrint('DUCKAI LIVE CHECK PASSED: $output');
      }
    } catch (e) {
      if (mounted) {
        setState(() {
          output += '\n$e';
        });
      }
      debugPrint('DUCKAI LIVE CHECK FAILED: $e');
    } finally {
      await ai.close();
      client = null;
      if (mounted) {
        setState(() {
          cancellation = null;
        });
      }
    }
  }

  @override
  Widget build(BuildContext context) => Scaffold(
    appBar: AppBar(title: const Text('Duck.ai Dart SDK')),
    body: Padding(
      padding: const EdgeInsets.all(16),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          TextField(
            controller: prompt,
            decoration: const InputDecoration(labelText: 'Message'),
          ),
          const SizedBox(height: 16),
          Row(
            children: [
              FilledButton(
                onPressed: cancellation == null ? send : null,
                child: const Text('Send'),
              ),
              const SizedBox(width: 12),
              TextButton(
                onPressed: cancellation?.cancel,
                child: const Text('Cancel'),
              ),
            ],
          ),
          const SizedBox(height: 16),
          Expanded(
            child: SingleChildScrollView(
              child: Text(output, key: const Key('response')),
            ),
          ),
        ],
      ),
    ),
  );
}
