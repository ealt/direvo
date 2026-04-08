import initSqlJs, { type Database, type SqlJsStatic } from "sql.js";

let SQL: SqlJsStatic | null = null;

async function getSql(): Promise<SqlJsStatic> {
  if (!SQL) {
    SQL = await initSqlJs({
      locateFile: (file: string) =>
        `https://sql.js.org/dist/${file}`,
    });
  }
  return SQL;
}

export async function loadDatabase(url: string): Promise<Database> {
  const sql = await getSql();
  const response = await fetch(url);
  if (!response.ok) {
    throw new Error(`Failed to fetch ${url}: ${response.status}`);
  }
  const buffer = await response.arrayBuffer();
  return new sql.Database(new Uint8Array(buffer));
}

export type { Database };
