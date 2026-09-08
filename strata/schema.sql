create table if not exists documents (
    id integer primary key,
    sha256 text not null unique,
    filename text not null,
    pdf blob,
    title text,
    publisher text,
    published_at text,
    page_count integer not null default 0,
    status text not null default 'queued',
    malformed_lines integer not null default 0,
    ingested_at text
);

create table if not exists pages (
    doc_id integer not null references documents(id),
    page_no integer not null,
    text text not null default '',
    word_count integer not null default 0,
    hints_json text not null default '{}',
    skipped integer not null default 0,
    primary key (doc_id, page_no)
);

create table if not exists claims (
    id integer primary key,
    doc_id integer not null references documents(id),
    page_no integer not null,
    subject text,
    metric_raw text,
    label text,
    value_raw text,
    unit_raw text,
    period_raw text,
    scope_raw text,
    basis_raw text,
    quote text,
    keys_json text not null default '{}',
    status text not null default 'unverified'
);

create index if not exists claims_doc on claims(doc_id, page_no);

create table if not exists evidence (
    claim_id integer primary key references claims(id),
    page_no integer not null,
    char_start integer,
    char_end integer,
    bbox_json text,
    grade text not null
);

create table if not exists quarantine (
    claim_id integer primary key references claims(id),
    reason text not null,
    detail text
);

create table if not exists entities (
    id integer primary key,
    name_canon text not null unique,
    kind text,
    keys_json text not null default '{}'
);

create table if not exists metrics (
    key text primary key,
    label text,
    first_seen_doc integer references documents(id),
    aliases_json text not null default '[]',
    claim_count integer not null default 0
);

create table if not exists claim_canon (
    claim_id integer primary key references claims(id),
    entity_id integer references entities(id),
    metric_key text references metrics(key),
    period_start text,
    period_end text,
    unit_canon text,
    value_canon real,
    value_text text,
    basis_canon text,
    scope_canon text,
    block_key text,
    precision real
);

create index if not exists claim_canon_block on claim_canon(block_key);

create table if not exists relations (
    id integer primary key,
    a_id integer not null references claims(id),
    b_id integer not null references claims(id),
    kind text not null,
    dimension text,
    explanation text,
    confidence text,
    unique (a_id, b_id)
);

create index if not exists relations_kind on relations(kind);
