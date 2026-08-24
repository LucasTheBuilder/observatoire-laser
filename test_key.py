from dotenv import load_dotenv
load_dotenv()
import os

key = os.getenv("ANTHROPIC_API_KEY", "")
print("Longueur :", len(key))
print("Début :", repr(key[:12]))
print("Fin :", repr(key[-6:]))