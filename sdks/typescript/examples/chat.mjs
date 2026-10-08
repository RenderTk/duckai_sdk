import { DuckAI, Model } from "@rendertk/duckai-sdk";

const ai = new DuckAI({ model: Model.GPT_6_LUNA });
try {
  const conversation = ai.conversation();
  for await (const delta of conversation.stream(
    "Explain recursion with a short JavaScript example.",
  )) {
    process.stdout.write(delta);
  }
  console.log(
    "\n",
    (await conversation.chat("How does the base case work?")).text,
  );
} finally {
  await ai.close();
}
