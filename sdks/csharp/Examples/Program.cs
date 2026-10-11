using DuckAI;
await using var ai = new DuckAIClient(Model.MISTRAL_SMALL_4);
var chat = ai.Conversation();
await foreach (var delta in chat.Stream("Explain recursion with a short example.")) Console.Write(delta);
Console.WriteLine();
var response = await chat.ChatAsync("Now make the example simpler.");
Console.WriteLine(response.Text);
if (!response.Done || string.IsNullOrEmpty(response.Text) || chat.Messages.Count != 4) throw new Exception("Conversation did not complete");
Console.WriteLine("LIVE CHECK PASSED");
