from db import reset_market_database

if __name__ == "__main__":
    backup = reset_market_database(backup=True)
    print("market.db reconstruit.")
    if backup:
        print(f"Sauvegarde précédente : {backup}")
