// A different module entirely. Its `uid` is a real datasource uid at runtime, and the corpus-wide
// property index used to fill it in with the sentinel from the file next door, producing
// `/api/datasources/uid/-- Mixed --/health`, an address that does not exist.
export function health(ds) { return fetch(`/api/datasources/uid/${ds.uid}/health`); }
export function teams(ds) { return fetch(`/api/datasources/uid/${ds.uid}/lbac/teams`); }
