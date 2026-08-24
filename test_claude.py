from dotenv import load_dotenv
load_dotenv()


from hybrid import get_ai_client

client = get_ai_client()
print("Client utilisé :", type(client).__name__)
print("Disponible :", client.available())

if client.available():
    reponse = client.ask_json(
        system="Réponds uniquement en JSON.",
        prompt='Renvoie {"test": "ok", "nombre": 42}'
    )
    print("Réponse :", reponse)