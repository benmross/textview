import { statSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";

const identity = (path) => {
  const stat = statSync(path, { bigint: true });
  return `${stat.dev}:${stat.ino}`;
};

const closeQuietly = (database) => {
  try {
    database?.close();
  } catch {
    // The process can still safely discard a failed candidate connection.
  }
};

export class DatabaseSnapshot {
  #archivePath;
  #indexPath;
  #archive;
  #appIndex;
  #indexIdentity;

  constructor(archivePath, indexPath) {
    this.#archivePath = archivePath;
    this.#indexPath = indexPath;
    this.#activate(this.#openPair());
  }

  get archive() {
    return this.#archive;
  }

  get appIndex() {
    return this.#appIndex;
  }

  get indexIdentity() {
    return this.#indexIdentity;
  }

  refreshIfReplaced() {
    if (identity(this.#indexPath) === this.#indexIdentity) return false;
    this.#activate(this.#openPair());
    return true;
  }

  close() {
    closeQuietly(this.#archive);
    closeQuietly(this.#appIndex);
    this.#archive = null;
    this.#appIndex = null;
  }

  #activate(next) {
    const previousArchive = this.#archive;
    const previousAppIndex = this.#appIndex;
    this.#archive = next.archive;
    this.#appIndex = next.appIndex;
    this.#indexIdentity = next.indexIdentity;
    closeQuietly(previousArchive);
    closeQuietly(previousAppIndex);
  }

  #openPair() {
    // The index builder publishes with an atomic rename. Checking its identity
    // before and after both opens guarantees that the direct connection and
    // the archive's attached `app` database refer to the same generation.
    for (let attempt = 0; attempt < 3; attempt += 1) {
      const before = identity(this.#indexPath);
      let archive;
      let appIndex;
      try {
        archive = new DatabaseSync(this.#archivePath, { readOnly: true });
        appIndex = new DatabaseSync(this.#indexPath, { readOnly: true });
        const escapedIndex = this.#indexPath.replaceAll("'", "''");
        archive.exec(`ATTACH DATABASE 'file:${escapedIndex}?mode=ro' AS app`);
        archive.exec(
          "PRAGMA query_only=ON; PRAGMA cache_size=-16384; PRAGMA mmap_size=67108864"
        );
        appIndex.exec("PRAGMA query_only=ON; PRAGMA cache_size=-16384");
        const after = identity(this.#indexPath);
        if (before === after) {
          return { archive, appIndex, indexIdentity: after };
        }
      } catch (error) {
        closeQuietly(archive);
        closeQuietly(appIndex);
        if (identity(this.#indexPath) === before) throw error;
        continue;
      }
      closeQuietly(archive);
      closeQuietly(appIndex);
    }
    throw new Error("TextView index changed repeatedly while being opened");
  }
}
