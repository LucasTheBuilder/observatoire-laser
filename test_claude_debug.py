from dotenv import load_dotenv
load_dotenv()

import anthropic

client = anthropic.Anthropic()  # lit ANTHROPIC_API_KEY dans l'environnement automatiquement

response = client.messages.create(
    model="claude-haiku-4-5",
    max_tokens=100,
    messages=[{"role": "user", "content": "Réponds juste le mot ok"}],
)
print(response.content)