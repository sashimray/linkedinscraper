import sqlite3
import sqlite_vec

db = sqlite3.connect("my_database.db")
db.enable_load_extension(True)
sqlite_vec.load(db)
db.enable_load_extension(False)

# Verify installation
version = db.execute("SELECT vec_version()").fetchone()[0]
print(f"sqlite-vec version: {version}")
