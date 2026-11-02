use anyhow::{Context, Result};
use rusqlite::{params, Connection};
use serde::Serialize;
use std::{
    path::Path,
    time::{SystemTime, UNIX_EPOCH},
};
#[derive(Debug, Serialize)]
pub struct Entry {
    pub id: i64,
    pub ts: i64,
    pub text: String,
    pub app: String,
    pub duration_ms: u64,
    pub latency_ms: u64,
    pub model: String,
}
pub struct History {
    connection: Connection,
}
impl History {
    pub fn open(path: &Path) -> Result<Self> {
        let connection = Connection::open(path).context("Open history database")?;
        connection
            .execute_batch(
                "PRAGMA secure_delete=ON; PRAGMA journal_mode=DELETE;
            CREATE TABLE IF NOT EXISTS entries (
                id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, text TEXT NOT NULL,
                app TEXT NOT NULL, duration_ms INTEGER NOT NULL, latency_ms INTEGER NOT NULL, model TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS entries_ts ON entries(ts);",
            )
            .context("Initialize history schema")?;
        Ok(Self { connection })
    }
    pub fn insert(&self, text: &str, app: &str, duration_ms: u64, latency_ms: u64, model: &str) -> Result<()> {
        let ts = SystemTime::now().duration_since(UNIX_EPOCH).context("System clock precedes epoch")?.as_secs();
        self.connection
            .execute(
                "INSERT INTO entries(ts,text,app,duration_ms,latency_ms,model) VALUES (?1,?2,?3,?4,?5,?6)",
                params![ts, text, app, duration_ms, latency_ms, model],
            )
            .context("Save transcript history")?;
        Ok(())
    }
    pub fn search(&self, q: &str, limit: u32) -> Result<Vec<Entry>> {
        let mut statement = self
            .connection
            .prepare(
                "SELECT id,ts,text,app,duration_ms,latency_ms,model
            FROM entries WHERE instr(lower(text),lower(?1)) > 0 ORDER BY ts DESC,id DESC LIMIT ?2",
            )
            .context("Prepare history search")?;
        let rows = statement
            .query_map(params![q, limit.min(500)], |r| {
                Ok(Entry {
                    id: r.get(0)?,
                    ts: r.get(1)?,
                    text: r.get(2)?,
                    app: r.get(3)?,
                    duration_ms: r.get(4)?,
                    latency_ms: r.get(5)?,
                    model: r.get(6)?,
                })
            })
            .context("Search history")?;
        rows.collect::<std::result::Result<Vec<_>, _>>().context("Read history entries")
    }
    pub fn prune(&self, days: u32) -> Result<()> {
        let now = SystemTime::now().duration_since(UNIX_EPOCH).context("Read system time")?.as_secs();
        self.connection
            .execute("DELETE FROM entries WHERE ts < ?1", [now.saturating_sub(u64::from(days) * 86_400)])
            .context("Prune expired history")?;
        Ok(())
    }
    pub fn clear(&self) -> Result<()> {
        self.connection.execute_batch("DELETE FROM entries; VACUUM;").context("Erase transcript history")?;
        Ok(())
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn crud_and_literal_search() -> Result<()> {
        let h = History::open(Path::new(":memory:"))?;
        h.insert("Hello 100%", "editor", 1000, 30, "tiny.en")?;
        assert_eq!(h.search("HELLO", 10)?.len(), 1);
        assert_eq!(h.search("%", 10)?.len(), 1);
        assert!(h.search("_", 10)?.is_empty());
        assert!(h.search("", 0)?.is_empty());
        h.clear()?;
        assert!(h.search("", 20)?.is_empty());
        Ok(())
    }
    #[test]
    fn retention() -> Result<()> {
        let h = History::open(Path::new(":memory:"))?;
        h.insert("old", "app", 1, 1, "tiny.en")?;
        h.connection.execute("UPDATE entries SET ts=1", [])?;
        h.insert("new", "app", 1, 1, "tiny.en")?;
        h.prune(7)?;
        let rows = h.search("", 10)?;
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].text, "new");
        Ok(())
    }
}
