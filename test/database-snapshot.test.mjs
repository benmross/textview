import assert from "node:assert/strict";
import { mkdtempSync, renameSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { DatabaseSync } from "node:sqlite";
import test from "node:test";
import { DatabaseSnapshot } from "../lib/database-snapshot.mjs";

const createArchive = (path) => {
  const database = new DatabaseSync(path);
  database.exec("CREATE TABLE messages(id INTEGER PRIMARY KEY)");
  database.close();
};

const publishIndex = (path, generation) => {
  const building = `${path}.building`;
  const database = new DatabaseSync(building);
  database.exec("CREATE TABLE generation(value INTEGER NOT NULL)");
  database.prepare("INSERT INTO generation VALUES(?)").run(generation);
  database.close();
  renameSync(building, path);
};

test("reloads both index connections after an atomic replacement", () => {
  const directory = mkdtempSync(join(tmpdir(), "textview-index-reload-"));
  const archivePath = join(directory, "messages.db");
  const indexPath = join(directory, "textview-index.db");
  let snapshot;
  try {
    createArchive(archivePath);
    publishIndex(indexPath, 1);
    snapshot = new DatabaseSnapshot(archivePath, indexPath);

    assert.equal(
      snapshot.appIndex.prepare("SELECT value FROM generation").get().value,
      1
    );
    assert.equal(
      snapshot.archive.prepare("SELECT value FROM app.generation").get().value,
      1
    );
    assert.equal(snapshot.refreshIfReplaced(), false);

    publishIndex(indexPath, 2);
    assert.equal(snapshot.refreshIfReplaced(), true);
    assert.equal(
      snapshot.appIndex.prepare("SELECT value FROM generation").get().value,
      2
    );
    assert.equal(
      snapshot.archive.prepare("SELECT value FROM app.generation").get().value,
      2
    );
  } finally {
    snapshot?.close();
    rmSync(directory, { recursive: true, force: true });
  }
});
