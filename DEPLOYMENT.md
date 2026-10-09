# Deployment Guide: MongoDB Atlas → Snowflake with Openflow (Snowflake Deployment)

A step-by-step runbook to replicate MongoDB collections into Snowflake in near real time with the **Openflow Connector for MongoDB**, running on Snowflake-managed compute.

The connector takes an initial snapshot of each collection, then streams inserts, updates and deletes from MongoDB change streams and merges them into Snowflake tables.

![Secure cross-cloud architecture: MongoDB Atlas to Snowflake with Openflow](images/architecture.png)

**Time required:** about 45–60 minutes. Most of it is waiting for the deployment and runtime to provision.

---

## Contents
1. [Prerequisites](#1-prerequisites)
2. [Choose your values](#2-choose-your-values)
3. [MongoDB Atlas setup](#3-mongodb-atlas-setup)
4. [Snowflake setup](#4-snowflake-setup)
5. [Deploy and configure the connector](#5-deploy-and-configure-the-connector)
6. [Validate](#6-validate)
7. [Optional: Continuous streaming test](#7-optional-continuous-streaming-test)
8. [Operations](#8-operations)
9. [Troubleshooting](#9-troubleshooting)
10. [Clean up](#10-clean-up)

---

## 1. Prerequisites

**MongoDB**
- MongoDB **4.4 or later**, deployed as a **replica set or sharded cluster**. Standalone instances are not supported, because change streams need an oplog. Any Atlas M10+ cluster qualifies.
- Atlas project access to manage Network Access and Database Access.

**Snowflake**
- An account where Openflow is available. An ORGADMIN must have accepted the Openflow terms (*Admin → Billing & Terms → Snowflake Feature Terms → Openflow*).
- `ACCOUNTADMIN`, or a role with the account-level grants in step 4.1.
- An existing warehouse for merge queries.

**Tools**
- A browser with access to **Snowsight** (for the SQL in section 4 and the Openflow UI in section 5).
- Optional: [`mongosh`](https://www.mongodb.com/try/download/shell) or MongoDB Compass to test the MongoDB user and generate changes.

> Sections 3–5 are all done through web UIs and SQL worksheets. No command-line tooling is required. Openflow menu labels can differ slightly between Snowsight releases; the steps below describe the standard layout.

---

## 2. Choose your values

Pick these once and substitute them throughout. The *Example* column shows the values from the reference deployment.

| Placeholder | Meaning | Example |
|---|---|---|
| `<ADMIN_ROLE>` | Openflow admin role | `OPENFLOW_ADMIN_RL` |
| `<RUNTIME_ROLE>` | Runtime execute-as role | `OPENFLOW_MONGODB_RUNTIME_RL` |
| `<INFRA_DB>.<INFRA_SCHEMA>` | Openflow infrastructure location | `OPENFLOW.OPENFLOW` |
| `<DEPLOYMENT>` | Openflow deployment name | `MONGODB_DEPLOYMENT` |
| `<RUNTIME>` | Openflow runtime name | `MONGODB_CDC_RUNTIME` |
| `<DEST_DB>` | Destination database | `MONGODB_RAW` |
| `<WAREHOUSE>` | Warehouse for merges | `DEMO_WH` |
| `<SF_USER>` | Your Snowflake user | `BSURESH` |
| `<SRV_HOST>` | Atlas SRV host (from the connection string) | `demo.mxicyu.mongodb.net` |
| `<HOST1..3>` | Replica set member hosts | `demo-shard-00-00.mxicyu.mongodb.net` … |
| `<REPLICA_SET>` | Replica set name | `atlas-2651ci-shard-0` |
| `<MONGO_USER>` | Connector database user | `openflow_cdc` |
| `<SOURCE_DB>` | MongoDB database to replicate | `retail` |
| `<SF_CLOUD>` / `<SF_REGION>` | Snowflake account cloud and region (`SELECT CURRENT_REGION();`) | AWS / us-east-1 |
| `<ATLAS_CLOUD>` / `<ATLAS_REGION>` | Atlas cluster cloud and region (cluster overview page) | GCP / us-east4 |

### Find the member hosts and replica set name
The Atlas connection string is `mongodb+srv://<SRV_HOST>/`. Resolve it:
```bash
dig +short SRV _mongodb._tcp.<SRV_HOST>      # → member hosts and ports (usually 27017)
dig +short TXT <SRV_HOST>                     # → replicaSet=<REPLICA_SET>&authSource=admin
```
You need these because the Openflow runtime **cannot resolve `mongodb+srv://` SRV records** (see [Troubleshooting](#9-troubleshooting)).

### Cloud regions and network architecture

The Openflow runtime runs inside your Snowflake account's cloud and region. The MongoDB cluster can be on any cloud. Connections always go **from the runtime to MongoDB**; MongoDB never connects to Snowflake. See the [architecture diagram](images/architecture.png) at the top of this guide.

```
 Snowflake account (<SF_CLOUD> / <SF_REGION>)                        MongoDB Atlas (<ATLAS_CLOUD> / <ATLAS_REGION>)
┌─────────────────────────────────────────────────┐          ┌─────────────────────────────────┐
│ Openflow runtime (Snowpark Container Services)    │          │ Replica set <REPLICA_SET>        │
│   egress allowed only by:                         │  TLS     │   <HOST1>:27017                  │
│   EAI → network rule (HOST_PORT, EGRESS)          │ ───────► │   <HOST2>:27017                  │
│   source IP = Snowflake egress range              │ public   │   <HOST3>:27017                  │
│                                                   │ internet │ Inbound allowed only by:         │
│ Writes to <DEST_DB> inside Snowflake (no egress)  │          │   IP Access List = egress ranges │
└─────────────────────────────────────────────────┘          └─────────────────────────────────┘
```

**Two controls must both allow the connection.** If either is missing, the connection times out with no clear error.

| Side | Control | What it allows | Set in |
|---|---|---|---|
| Snowflake (outbound) | Network rule + external access integration, attached to the runtime | The runtime may open connections to `<HOST1..3>:27017` | Section 4.5, 4.6 |
| MongoDB (inbound) | Atlas IP Access List | Connections from Snowflake's egress IP ranges | Section 3.1 |

**Connection details**

| Item | Value |
|---|---|
| Direction | Openflow runtime → MongoDB only |
| Protocol / port | MongoDB wire protocol over TLS, TCP **27017** (Atlas default) |
| Encryption | TLS, required by Atlas; set `tls=true` in the URI |
| Hosts | Each replica set member must be reachable. The driver discovers members and may connect to any of them. With `readPreference=secondaryPreferred`, reads go to secondaries. |
| DNS | Member hosts resolve to public IPs through normal A records. The SRV record behind `mongodb+srv://` **is not resolvable from the runtime**, so use the seed-list URI. |
| Snowflake side | Writes to the destination database stay inside Snowflake; no extra network rule is needed. |

**Choosing regions**
- Put the MongoDB cluster and the Snowflake account in the **same or a nearby region** to keep latency low. Cross-cloud is fully supported; in the reference deployment, Snowflake runs on AWS us-east-1 and Atlas on GCP us-east4, both in Northern Virginia.
- **Cross-cloud and cross-region traffic goes over the public internet**, protected by TLS and both allow-lists. Private connectivity options (AWS PrivateLink, Azure Private Link, GCP Private Service Connect) generally require both sides on the same cloud. Check current Snowflake and Atlas documentation if you need private connectivity.
- **Data transfer costs** can apply when traffic leaves a cloud or region. Snowflake may bill egress from the runtime (mostly small requests), and Atlas may bill outbound data from the cluster. The cluster's outbound data is the bulk of it: the initial snapshot plus every change. Check both providers' pricing for your regions.
- Snowflake's **egress IP ranges depend on your account's cloud and region**. Always read them from your own account with `SYSTEM$GET_SNOWFLAKE_EGRESS_IP_RANGES()`; don't copy another account's values.

---

## 3. MongoDB Atlas setup

### 3.1 Allow Snowflake's egress IPs
In Snowflake, run:
```sql
SELECT SYSTEM$GET_SNOWFLAKE_EGRESS_IP_RANGES();
```
In Atlas, go to **Security → Network Access → IP Access List → + Add IP Address** and add each returned `ipv4_prefix` (e.g. `153.45.64.0/24`) with the comment `Snowflake Openflow egress`. Wait until every entry shows **Active**.

> These ranges have an `expires` date. Re-check them before that date and update Atlas.

### 3.2 Create the connector user
1. **Security → Database Access → + Add New Database User**.
2. Authentication: **Password**, which uses SCRAM. X.509, LDAP and AWS IAM are not supported by the connector.
3. Username: `<MONGO_USER>`. Autogenerate a strong password and store it in a password manager.
4. **Built-in Role → Only read any database** (`readAnyDatabase@admin`). The connector reads change streams at **cluster** level, so database-scoped roles are not enough. Don't grant write or admin roles.
5. Optional: restrict the user to the target cluster.

### 3.3 Size the oplog window
**Clusters → … → Edit Configuration → Additional Settings → More Configuration Options → Set Minimum Oplog Window**, e.g. `24` hours or longer than your worst-case connector downtime. If the oplog rolls past the connector's resume point, collections must be fully re-synced.

### 3.4 Prepare source data
In **Data Explorer**, create `<SOURCE_DB>` and its collections, or use existing ones. Don't replicate `admin`, `local` or `config`.

### 3.5 Test the user (optional)
From a machine on the access list:
```bash
mongosh "mongodb+srv://<SRV_HOST>/?authSource=admin" --username <MONGO_USER>
```
```javascript
db.adminCommand({ connectionStatus: 1 }).authInfo   // shows the user and its roles
rs.status().ok                                       // 1 = healthy replica set
```

---

## 4. Snowflake setup

Run these as `ACCOUNTADMIN` in a Snowsight worksheet.

### 4.1 Admin role
```sql
CREATE ROLE IF NOT EXISTS <ADMIN_ROLE>
  COMMENT = 'Openflow admin role. [openflow]';
GRANT CREATE DATABASE, CREATE ROLE, CREATE EXTERNAL ACCESS INTEGRATION,
      CREATE OPENFLOW DEPLOYMENT, CREATE COMPUTE POOL
  ON ACCOUNT TO ROLE <ADMIN_ROLE>;
GRANT ROLE <ADMIN_ROLE> TO USER <SF_USER>;
GRANT ROLE <ADMIN_ROLE> TO ROLE ACCOUNTADMIN;
```

### 4.2 Infrastructure database and schema
```sql
CREATE DATABASE IF NOT EXISTS <INFRA_DB> COMMENT = 'Openflow infrastructure. [openflow]';
CREATE SCHEMA  IF NOT EXISTS <INFRA_DB>.<INFRA_SCHEMA> COMMENT = 'Openflow infrastructure. [openflow]';
```

### 4.3 Openflow deployment (5–10 min)
```sql
SHOW OPENFLOW DEPLOYMENTS;      -- reuse an existing one if appropriate
CREATE OPENFLOW DEPLOYMENT <DEPLOYMENT> COMMENT = 'Openflow deployment for MongoDB. [openflow]';
SELECT SYSTEM$WAIT_FOR_STABLE_OPENFLOW_DEPLOYMENTS(600, '<DEPLOYMENT>');
```
If the wait times out, re-run the `SELECT`. Don't run the `CREATE` again.

### 4.4 Destination database and runtime role
```sql
CREATE DATABASE IF NOT EXISTS <DEST_DB> COMMENT = 'MongoDB replication destination. [openflow]';
CREATE ROLE IF NOT EXISTS <RUNTIME_ROLE> COMMENT = 'Openflow MongoDB runtime role. [openflow]';

GRANT ROLE <RUNTIME_ROLE> TO ROLE <ADMIN_ROLE>;
GRANT USAGE, CREATE SCHEMA ON DATABASE <DEST_DB>       TO ROLE <RUNTIME_ROLE>;
GRANT USAGE, OPERATE ON WAREHOUSE <WAREHOUSE>          TO ROLE <RUNTIME_ROLE>;
GRANT USAGE ON DATABASE <INFRA_DB>                      TO ROLE <RUNTIME_ROLE>;
GRANT USAGE ON SCHEMA <INFRA_DB>.<INFRA_SCHEMA>         TO ROLE <RUNTIME_ROLE>;
```

### 4.5 Network rule and external access integration
List the **member hosts**, not the SRV host. The SRV host has no A record, so the statement fails with "unresolvable host name".
```sql
CREATE NETWORK RULE IF NOT EXISTS <INFRA_DB>.<INFRA_SCHEMA>.MONGODB_OPENFLOW_NETWORK_RULE
  TYPE = HOST_PORT MODE = EGRESS
  VALUE_LIST = ('<HOST1>:27017', '<HOST2>:27017', '<HOST3>:27017')
  COMMENT = 'Openflow MongoDB egress. [openflow]';

CREATE EXTERNAL ACCESS INTEGRATION IF NOT EXISTS MONGODB_OPENFLOW_EAI
  ALLOWED_NETWORK_RULES = (<INFRA_DB>.<INFRA_SCHEMA>.MONGODB_OPENFLOW_NETWORK_RULE)
  ENABLED = TRUE
  COMMENT = 'Openflow MongoDB connectivity. [openflow]';

GRANT USAGE ON INTEGRATION MONGODB_OPENFLOW_EAI TO ROLE <RUNTIME_ROLE>;

-- Configuration check: expect "allowed": true (this does not test actual reachability)
SELECT SYSTEM$VERIFY_EAI_NETWORK_ACCESS('MONGODB_OPENFLOW_EAI', '<HOST1>', 27017);
```

> Never use `CREATE OR REPLACE` on network rules or integrations. It silently drops existing grants. Use `ALTER` to change them.

### 4.6 Runtime (5–10 min)
The connector needs a **single-node** runtime of at least **Medium** size; use Large for high throughput. **Size cannot be changed after creation.**
```sql
CREATE OPENFLOW RUNTIME <INFRA_DB>.<INFRA_SCHEMA>.<RUNTIME>
  IN DEPLOYMENT <DEPLOYMENT>
  MIN_NODES = 1 MAX_NODES = 1
  NODE_TYPE = MEDIUM
  EXECUTE_AS_ROLE = '<RUNTIME_ROLE>'
  COMMENT = 'Openflow runtime for MongoDB CDC. [openflow]';
SELECT SYSTEM$WAIT_FOR_STABLE_OPENFLOW_RUNTIMES(600, '<INFRA_DB>.<INFRA_SCHEMA>.<RUNTIME>');

ALTER OPENFLOW RUNTIME <INFRA_DB>.<INFRA_SCHEMA>.<RUNTIME>
  SET EXTERNAL_ACCESS_INTEGRATIONS = (MONGODB_OPENFLOW_EAI);   -- replaces the full list

DESCRIBE OPENFLOW RUNTIME <INFRA_DB>.<INFRA_SCHEMA>.<RUNTIME>;
```
From the `DESCRIBE` output, note:
- `status` = `ACTIVE`
- `external_access_integrations` includes `MONGODB_OPENFLOW_EAI`
- `server_url`, e.g. `https://of1--<account>.snowflakecomputing.app:443/<runtime-key>/nifi/`. This is the runtime canvas, which you can also reach from the Openflow UI in section 5.

---

## 5. Deploy and configure the connector

All steps in this section use the **Openflow UI** in Snowsight. Sign in with a role that can use the runtime, such as `ACCOUNTADMIN` or `<ADMIN_ROLE>`.

### 5.1 Open Openflow
1. In Snowsight, go to **Ingestion → Openflow** (in some releases: **Data → Openflow**).
2. Click **Launch Openflow**. The Openflow UI opens in a new tab.
3. Open the **Runtimes** tab and confirm `<RUNTIME>` shows **Active**.

### 5.2 Add the MongoDB connector to the runtime
1. In Openflow, open the connector catalog (**Overview → View more connectors**, or the **Connectors** tab).
2. Find **MongoDB** and click **Add to runtime**.
3. Select `<RUNTIME>` and click **Add**. Sign in again if prompted.
4. The runtime canvas opens with a new process group named **MongoDB**. Its components are stopped, which is expected at this point.

> If MongoDB opens a **Guided Wizard** instead of the canvas, your account offers it as a gen 2 connector. Enter the same values from 5.4 in the wizard and skip to section 6.

### 5.3 Decide the two settings you can't change later

These are fixed after the first start. Changing them later needs a full reset: stop the connector, clear its state, drop the destination tables and restart.

| Parameter | Options | Effect |
|---|---|---|
| `Object Identifier Resolution` | `CASE_INSENSITIVE` (default) | Uppercased names, e.g. `DEST.RETAIL.ORDERS`; queries need no quotes |
| | `CASE_SENSITIVE` | Source casing preserved, e.g. `DEST."retail"."orders"`; queries need quotes |
| `Destination Schema Pattern` | `${source.schema.name}` (default) | One Snowflake schema per MongoDB database |

### 5.4 Set parameters
On the canvas, right-click the **MongoDB** process group → **Parameters**. The connector has three parameter contexts; set each value in the context shown. To edit a parameter, click its **⋮** (or pencil) icon, enter the value, then **Apply**.

**MongoDB Source Parameters**

| Parameter | Value |
|---|---|
| MongoDB Connection URI | `mongodb://<HOST1>:27017,<HOST2>:27017,<HOST3>:27017/?tls=true&replicaSet=<REPLICA_SET>&readPreference=secondaryPreferred` |
| MongoDB Authentication Mechanism | `SCRAM-SHA-256` |
| MongoDB Username | `<MONGO_USER>` |
| MongoDB Password | the password from 3.2 (sensitive; it can't be viewed after saving) |
| MongoDB Authentication Source | `admin` |

**MongoDB Destination Parameters**

| Parameter | Value |
|---|---|
| Snowflake Authentication Strategy | `SNOWFLAKE_MANAGED` |
| Snowflake Role | `<RUNTIME_ROLE>` |
| Snowflake Warehouse | `<WAREHOUSE>` |
| Destination Database | `<DEST_DB>` |
| Destination Schema Pattern | leave the default `${source.schema.name}` |
| Account Identifier, Username, Private Key fields | leave empty |

**MongoDB Ingestion Parameters**

| Parameter | Value |
|---|---|
| Included Collection Regex | `<SOURCE_DB>\..*` (all collections in the database) |
| Included Collection Names | optional alternative, e.g. `retail.customers, retail.orders` |
| Object Identifier Resolution | your choice from 5.3 |
| Merge Task Schedule CRON | leave the default `* * * * * ?` |

Notes:
- **Don't put credentials in the URI.** The connector rejects it.
- Use the **seed-list `mongodb://` URI** with `tls=true` and `replicaSet`, not `mongodb+srv://`.
- If **neither** `Included Collection Regex` nor `Included Collection Names` is set, nothing is replicated. If both are set, the connector replicates the union.
- On Snowflake deployments, `SNOWFLAKE_MANAGED` is the only auth strategy needed.

### 5.5 Enable services and start
1. Right-click the **MongoDB** process group → **Enable all controller services**.
2. Optional check: right-click → **Controller Services**. Every service should be **Enabled**, except **Snowflake Private Key Service**, which shows as invalid because no private key is set. That's expected with managed auth and can be ignored.
3. Right-click the process group → **Start**.
4. After about a minute, check the process group's status bar: it should show running components and **no invalid (⚠) components**.
5. Check for errors: a red square on the process group means bulletins. Hover over it, or open **☰ → Bulletin Board**. Bulletins stay visible for about 5 minutes, so compare timestamps against when you started.

---

## 6. Validate

### 6.1 Initial snapshot
```sql
SELECT table_schema, table_name, row_count
FROM <DEST_DB>.INFORMATION_SCHEMA.TABLES
WHERE table_schema <> 'INFORMATION_SCHEMA'
ORDER BY 1, 2;
```
Expect one table per collection, plus internal `*_JOURNAL_*` tables. Don't modify or drop the journal tables.

### 6.2 Table shape
| Column | Content |
|---|---|
| `id` | MongoDB `_id` as text (primary key) |
| `data` | The full document (VARIANT) |
| `_SNOWFLAKE_DELETED` | Soft-delete flag |
| `_SNOWFLAKE_INSERTED_AT` / `_SNOWFLAKE_UPDATED_AT` | Connector timestamps |

```sql
-- CASE_INSENSITIVE example
SELECT id, data:name::string AS name
FROM <DEST_DB>.<SOURCE_DB>.<COLLECTION>
WHERE NOT _SNOWFLAKE_DELETED;

-- CASE_SENSITIVE example (quote identifiers)
SELECT * FROM <DEST_DB>."retail"."customers";
```
**Deletes are soft.** The row stays and the delete flag is set to TRUE. Filter on it to see only live documents.

### 6.3 Live change capture
```javascript
// in mongosh (as a user with write access)
use <SOURCE_DB>
db.customers.insertOne({ name: "Carol", city: "Brisbane" })
db.customers.updateOne({ name: "Bob" }, { $set: { tier: "gold" } })
db.orders.deleteOne({ customer: "Bob" })
```
Wait about 1 minute (the default merge schedule), then re-query Snowflake.

---

## 7. Optional: Continuous streaming test

> **Optional.** The deployment is complete after section 6. Use this only if you want to watch replication under a steady stream of changes, e.g. for a demo or a quick soak test.

`stream_to_mongo.py` in this repo writes a steady mix of operations (about 70% inserts, 25% updates, 5% deletes) to the `retail` database. Point `MONGO_URI` at your own cluster; to use a different database, change `client["retail"]` in the script.
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install pymongo dnspython
export MONGO_URI="mongodb+srv://<SRV_HOST>/?authSource=admin"
export MONGO_USER=<user-with-write-access>     # not the read-only connector user
export MONGO_PASSWORD='<password>'
python stream_to_mongo.py --interval 1 --batch 5      # Ctrl+C to stop; --max-batches N for a bounded run
```
While it runs, re-run the queries in 6.1–6.2. Row counts should keep up, trailing by about the merge interval (about 1 minute).

---

## 8. Operations

| Task | How |
|---|---|
| Runtime status | `SHOW OPENFLOW RUNTIMES IN ACCOUNT;` |
| Suspend / resume runtime | `ALTER OPENFLOW RUNTIME <INFRA_DB>.<INFRA_SCHEMA>.<RUNTIME> SUSPEND;` / `RESUME;` |
| Connector status | Runtime canvas → **MongoDB** process group status bar (running / stopped / invalid counts) |
| Errors | Red bulletin icon on the process group, or **☰ → Bulletin Board** |
| Stop / start connector | Right-click the process group → **Stop** / **Start** |
| Add collections | Process group → **Parameters** → update `Included Collection Regex` / `Included Collection Names` |
| Reduce warehouse cost | Process group → **Parameters** → set `Merge Task Schedule CRON` to a less frequent Quartz schedule, e.g. `0 */15 * * * ?` |
| Rotate MongoDB password | Change it in Atlas, then process group → **Parameters** → update `MongoDB Password` |

**Cautions**
- Keep connector downtime shorter than the oplog window.
- Never purge connector queues. Queued change events would be lost permanently.
- Don't edit or drop `*_JOURNAL_*` tables.
- Re-check the Snowflake egress IPs before they expire.

---

## 9. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `CREATE NETWORK RULE` fails: *unresolvable host name* | SRV host (`<SRV_HOST>`) has no A record | List only member hosts (`dig SRV`) |
| Bulletin: `Failed looking up SRV record for '_mongodb._tcp…'` | Runtime can't resolve `mongodb+srv://` | Use the seed-list `mongodb://h1,h2,h3/?tls=true&replicaSet=…` URI |
| `MongoTimeoutException` / `SocketTimeoutException` | Atlas IP Access List missing Snowflake egress IPs, or a host is missing from the network rule | Add the IP ranges in Atlas; add every member host to the rule |
| `UnknownHostException` | EAI not attached, not granted, or host missing | Check grants and `DESCRIBE OPENFLOW RUNTIME` (`external_access_integrations`) |
| Authentication failed | Wrong password or auth source | Check the user and password; `MongoDB Authentication Source` = `admin` |
| Controller invalid: credentials in URI | `user:pass@` in the URI | Remove it; use the Username/Password parameters |
| No tables created | Inclusion filters match nothing | Check `Included Collection Regex/Names` against `db.collection` |
| Destination write fails | Runtime role lacks privileges | Grant `USAGE, CREATE SCHEMA` on `<DEST_DB>` and `USAGE` on the warehouse |
| Collection `FAILED` after long downtime | Oplog rolled past the resume token | Re-sync the collection: remove it from the filter, drop its table, add it back; increase the oplog window |
| `Snowflake Private Key Service` invalid | Expected with managed auth | Ignore |
| Can't open the runtime canvas, or it asks you to sign in repeatedly | Role lacks usage on the runtime, or the session expired | Sign in with `ACCOUNTADMIN` / `<ADMIN_ROLE>`; refresh the Openflow tab |
| `USE ROLE` not allowed | Session restricted to one role | Run the setup as `ACCOUNTADMIN` (which inherits `<ADMIN_ROLE>`) |

---

## 10. Clean up

Run these in order, and only when you want to remove everything. **The `DROP DATABASE <DEST_DB>` step deletes the replicated data.**

1. On the runtime canvas, right-click the **MongoDB** process group → **Stop**, then **Disable all controller services**.
2. Right-click → **Delete**. If NiFi refuses because queues aren't empty, right-click → **Empty all queues** first. Only do this when removing the connector: queued changes are discarded.
3. Then run in a worksheet:
```sql
ALTER OPENFLOW RUNTIME <INFRA_DB>.<INFRA_SCHEMA>.<RUNTIME> TERMINATE;
DROP OPENFLOW RUNTIME <INFRA_DB>.<INFRA_SCHEMA>.<RUNTIME>;
ALTER OPENFLOW DEPLOYMENT <DEPLOYMENT> TERMINATE;
DROP OPENFLOW DEPLOYMENT <DEPLOYMENT>;
DROP INTEGRATION MONGODB_OPENFLOW_EAI;
DROP NETWORK RULE <INFRA_DB>.<INFRA_SCHEMA>.MONGODB_OPENFLOW_NETWORK_RULE;
DROP DATABASE <DEST_DB>;          -- deletes replicated data
DROP ROLE <RUNTIME_ROLE>;
```
In Atlas, remove the connector user and the Snowflake IP entries.

---

## Appendix A — Reference deployment (example values)

| Item | Value |
|---|---|
| Snowflake account | `SFSEAPAC-BSURESH` (AWS us-east-1) |
| Atlas cluster | `demo`, M10, MongoDB 8.0, GCP us-east4, 3-node replica set `atlas-2651ci-shard-0` |
| Deployment / runtime | `MONGODB_DEPLOYMENT` / `OPENFLOW.OPENFLOW.MONGODB_CDC_RUNTIME` (Medium) |
| Connector | MongoDB connector v0.27.0 |
| Collections | `retail\..*` → `MONGODB_RAW."retail"."customers"`, `"orders"` (CASE_SENSITIVE) |
| Result | Snapshot replicated (2 + 2 rows); 47 processors running, 0 invalid |
| Change capture test | Ran `stream_to_mongo.py`. Snowflake showed `customers` 51 rows (6 updated) and `orders` 50 rows (2 updated, 2 soft-deleted). Inserts, updates and deletes all replicated over public connectivity, without Private Link. |

### A.1 Cloud regions

| Component | Cloud | Region | Location |
|---|---|---|---|
| Snowflake account `SFSEAPAC-BSURESH` (Openflow deployment, runtime, destination DB) | AWS | `us-east-1` (`PUBLIC.AWS_US_EAST_1`) | N. Virginia |
| MongoDB Atlas cluster `demo` | GCP | `us-east4` | N. Virginia |

This is a **cross-cloud** path (AWS → GCP) within the same metro area. Traffic goes over the public internet using TLS.

### A.2 Network configuration applied

**Snowflake (outbound)**

| Object | Setting |
|---|---|
| Network rule `OPENFLOW.OPENFLOW.MONGODB_OPENFLOW_NETWORK_RULE` | `TYPE = HOST_PORT`, `MODE = EGRESS` |
| Allowed hosts | `demo-shard-00-00.mxicyu.mongodb.net:27017`, `demo-shard-00-01.mxicyu.mongodb.net:27017`, `demo-shard-00-02.mxicyu.mongodb.net:27017` |
| External access integration | `MONGODB_OPENFLOW_EAI` (enabled), `USAGE` granted to `OPENFLOW_MONGODB_RUNTIME_RL` |
| Attached to runtime | `OPENFLOW.OPENFLOW.MONGODB_CDC_RUNTIME` |
| Verification | `SYSTEM$VERIFY_EAI_NETWORK_ACCESS` returned `allowed: true` (matched rule above) |

**Snowflake egress IPs** (from `SYSTEM$GET_SNOWFLAKE_EGRESS_IP_RANGES()` on this account)

| CIDR | Effective | Expires |
|---|---|---|
| `153.45.64.0/24` | 2025-08-01 | 2027-01-07 |
| `153.45.72.0/24` | 2025-08-01 | 2027-01-07 |

**MongoDB Atlas (inbound)**

| Setting | Value |
|---|---|
| IP Access List | `153.45.64.0/24`, `153.45.72.0/24` (comment "Snowflake Openflow egress"), plus the admin laptop's IP for testing |
| Port / TLS | 27017, TLS required |
| SRV host | `demo.mxicyu.mongodb.net`, an SRV/TXT record only with no A record |
| SRV lookup result | 3 members on 27017; TXT `authSource=admin&replicaSet=atlas-2651ci-shard-0` |

**Connector URI used**
```
mongodb://demo-shard-00-00.mxicyu.mongodb.net:27017,demo-shard-00-01.mxicyu.mongodb.net:27017,demo-shard-00-02.mxicyu.mongodb.net:27017/?tls=true&replicaSet=atlas-2651ci-shard-0&readPreference=secondaryPreferred
```

### A.3 Networking issues hit, in order

1. **Network rule creation failed.** Including `demo.mxicyu.mongodb.net:27017` returned *"unresolvable host name"* because the SRV host has no A record. **Fix:** list only the three member hosts.
2. **Connector couldn't find the cluster.** With `mongodb+srv://demo.mxicyu.mongodb.net/`, bulletins showed `Failed looking up SRV record for '_mongodb._tcp.demo.mxicyu.mongodb.net'` and `MongoTimeoutException`. **Fix:** switch to the seed-list URI above. The snapshot then completed within about 2 minutes.
3. **Local mongosh test hung.** The laptop's IP wasn't on the Atlas IP Access List. **Fix:** add the current IP in Atlas Network Access. This only affects testing from a workstation, not the connector.

## Appendix B — References
- [About the Openflow Connector for MongoDB](https://docs.snowflake.com/en/user-guide/data-integration/openflow/connectors/mongodb/about)
- [Connect to MongoDB](https://docs.snowflake.com/en/user-guide/data-integration/openflow/connectors/mongodb/connect)
- [Set up the connector](https://docs.snowflake.com/en/user-guide/data-integration/openflow/connectors/mongodb/setup)
- [Use the connector](https://docs.snowflake.com/en/user-guide/data-integration/openflow/connectors/mongodb/use)
- [Openflow allow-list for Snowflake deployments](https://docs.snowflake.com/en/user-guide/data-integration/openflow/setup-openflow-spcs-sf-allow-list)
