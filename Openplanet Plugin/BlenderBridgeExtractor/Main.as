// Blender Bridge Extractor
//
// Extracts game files (including files inside the game's .pak archives) for
// the Trackmania Blender Bridge addon, using Openplanet's own Fids::Extract,
// the same call the Nod Explorer "Extract" button makes.
//
// Blender runs a small HTTP API on 127.0.0.1 (port 1337 by default). This
// plugin long-polls GET /api/plugin/next for jobs:
//   {"id", "kind": "extract", "paths": [...]}  extract files, or whole
//                                               folders for paths ending in "/"
//   {"id", "kind": "index", "paths": []}        list every GameData file
// Files land in the current Openplanet data folder's Extract/<path>, used as
// game library. Index chunks go to POST /api/plugin/jobs/<id>/index as
// "path<TAB>size" lines, results to POST /api/plugin/jobs/<id>/result as
// "STATUS<TAB>path<TAB>detail" lines (OK, SIZE, FAIL or NOTFOUND). While a
// job runs, POST /api/plugin/jobs/<id>/progress ("done<TAB>total<TAB>message")
// tells Blender the plugin is still alive.
//
// There is no window: enable the plugin and forget it. Settings live in
// Openplanet's Settings dialog; activity is reported as notifications. Jobs
// that arrive back to back (one map import) share one start and one summary
// notification.

[Setting category="General" name="Serve requests from Blender"]
bool S_Enabled = true;

[Setting category="General" name="Blender bridge URL"]
string S_ServerUrl = "http://127.0.0.1:1337";

[Setting category="Notifications" name="Show notifications"]
bool S_Notify = true;

[Setting category="Notifications" name="Only show problems" description="Hide connection and success notifications."]
bool S_NotifyProblemsOnly = false;

const string PLUGIN_VERSION = "2.1.6";
const string TITLE = "Blender Bridge";
const uint INDEX_CHUNK = 5000;
// Seconds to wait for a follow-up job before a batch counts as finished.
const int BATCH_GAP = 3;
// A lost connection is only announced after this many ms, so a bridge restart
// (toggle, port change) that reconnects in time stays silent.
const uint64 DISCONNECT_GRACE = 6000;
const vec4 COLOR_OK = vec4(0.13f, 0.45f, 0.20f, 1.0f);
const vec4 COLOR_ERROR = vec4(0.65f, 0.15f, 0.15f, 1.0f);

bool g_connected = false;
// What the user was last told: "Connected to Blender" shown and not yet
// followed by "Blender disconnected".
bool g_announced = false;
uint64 g_lostAt = 0;
Net::HttpRequest@ g_ping;
uint64 g_lastPing = 0;

// Current batch of back-to-back jobs.
bool g_inBatch = false;
uint g_batchOk = 0;
uint g_batchFailed = 0;
string g_batchFirstFailure = "";
string g_batchIndex = "";

void Notify(const string &in text) {
    if (S_Notify && !S_NotifyProblemsOnly) UI::ShowNotification(TITLE, text);
}

void NotifySuccess(const string &in text) {
    if (S_Notify && !S_NotifyProblemsOnly) UI::ShowNotification(TITLE, text, COLOR_OK, 5000);
}

void NotifyProblem(const string &in text) {
    warn(text);
    if (S_Notify) UI::ShowNotification(TITLE, text, COLOR_ERROR, 10000);
}

string ServerUrl() {
    string url = S_ServerUrl.Trim();
    while (url.EndsWith("/")) url = url.SubStr(0, url.Length - 1);
    return url;
}

string GameVersion() {
    string version = "";
#if TMNEXT
    {
    auto app = GetApp();
    if (app !is null && app.SystemPlatform !is null) version = app.SystemPlatform.ExeVersion;
    }
#endif
#if MP4
    {
    auto app = cast<CGameManiaPlanet>(GetApp());
    if (app !is null && app.ManiaPlanetScriptAPI !is null) version = app.ManiaPlanetScriptAPI.ExeVersion;
    }
#endif
#if MP41
    {
    auto app = cast<CGameManiaPlanet>(GetApp());
    if (app !is null && app.ManiaPlanetScriptAPI !is null) version = app.ManiaPlanetScriptAPI.ExeVersion;
    }
#endif
    return version;
}

Net::HttpRequest@ Finish(Net::HttpRequest@ req) {
    while (!req.Finished()) yield();
    return req;
}

bool Post(const string &in route, const string &in body) {
    auto req = Finish(Net::HttpPost(ServerUrl() + route, body, "text/plain; charset=utf-8"));
    int code = req.ResponseCode();
    if (code < 200 || code >= 300) {
        warn("POST " + route + " -> " + code + " " + req.Error());
        return false;
    }
    return true;
}

// Fire-and-forget progress report, at most every 2 seconds.
void Heartbeat(const string &in id, uint done, uint total, const string &in message) {
    if (id.Length == 0) return;
    if (g_ping !is null && !g_ping.Finished()) return;
    if (Time::Now - g_lastPing < 2000) return;
    g_lastPing = Time::Now;
    @g_ping = Net::HttpPost(ServerUrl() + "/api/plugin/jobs/" + id + "/progress",
        "" + done + "\t" + total + "\t" + message, "text/plain; charset=utf-8");
}

void Main() {
    startnew(WatchConnection);
    while (true) {
        if (!S_Enabled) {
            EndBatch();
            Pause();
            sleep(1000);
            continue;
        }
        int wait = g_inBatch ? BATCH_GAP : 15;
        string url = ServerUrl() + "/api/plugin/next?wait=" + wait + "&plugin=" + PLUGIN_VERSION
            + "&game=" + Net::UrlEncode(GameVersion());
        auto req = Finish(Net::HttpGet(url));
        int code = req.ResponseCode();
        // Serving was switched off during the long poll: drop the answer
        // (a job taken here is handed out again by Blender after its timeout).
        if (!S_Enabled) continue;
        if (code == 204) {
            SetConnected(true);
            EndBatch();
            continue;
        }
        if (code != 200) {
            EndBatch();
            SetConnected(false);
            sleep(3000);
            continue;
        }
        SetConnected(true);
        Json::Value@ job = Json::Parse(req.String());
        if (job is null || job.GetType() != Json::Type::Object || !job.HasKey("id")) {
            warn("Malformed job: " + req.String());
            sleep(1000);
            continue;
        }
        RunJob(job);
    }
}

void SetConnected(bool connected) {
    if (connected == g_connected) return;
    g_connected = connected;
    if (connected) {
        print("Connected to Blender (" + ServerUrl() + ")");
        if (!g_announced) {
            g_announced = true;
            Notify("Connected to Blender");
        }
    } else {
        print("Blender not reachable (" + ServerUrl() + ")");
        g_lostAt = Time::Now;
    }
}

// Serving switched off in the plugin settings: forget the connection quietly.
void Pause() {
    g_connected = false;
    g_announced = false;
}

void WatchConnection() {
    while (true) {
        if (g_announced && !g_connected && S_Enabled && Time::Now - g_lostAt >= DISCONNECT_GRACE) {
            g_announced = false;
            Notify("Blender disconnected");
        }
        sleep(500);
    }
}

void BeginBatch(const string &in text) {
    if (g_inBatch) return;
    g_inBatch = true;
    g_batchOk = 0;
    g_batchFailed = 0;
    g_batchFirstFailure = "";
    g_batchIndex = "";
    Notify(text);
}

void EndBatch() {
    if (!g_inBatch) return;
    g_inBatch = false;
    if (g_batchIndex.Length > 0) NotifySuccess(g_batchIndex);
    uint total = g_batchOk + g_batchFailed;
    if (total == 0) return;
    if (g_batchFailed > 0) {
        NotifyProblem("Extracted " + g_batchOk + " of " + total + " files, " + g_batchFailed
            + " failed\n" + g_batchFirstFailure + "\nSee Openplanet.log for all failures.");
    } else {
        NotifySuccess("Extracted " + total + (total == 1 ? " file" : " files") + " for Blender");
    }
}

void RunJob(Json::Value@ job) {
    string id = job["id"];
    string kind = job.HasKey("kind") ? string(job["kind"]) : "extract";
    array<string> paths;
    if (job.HasKey("paths")) {
        Json::Value@ list = job["paths"];
        for (uint i = 0; i < list.Length; i++) paths.InsertLast(string(list[i]));
    }
    if (kind == "index") {
        BeginBatch("Indexing game files for Blender...");
        RunIndex(id);
    } else if (kind == "extract" || kind == "extract_hook_tree" || kind == "extract_hook") {
        bool hook = kind != "extract";
        string suffix = kind == "extract_hook_tree" ? ".vegettreemodel.gbx" : ".gbx";
        if (hook && (paths.Length != 1 || !paths[0].ToLower().EndsWith(suffix))) {
            Post("/api/plugin/jobs/" + id + "/result", "FAIL\tGameData/\thook diagnostic requires exactly one matching GBX file");
            NotifyProblem("Rejected unsafe hook diagnostic request");
            return;
        }
        if (hook && NormalizePath(paths[0]).ToLower().EndsWith("/plugresource.gbx")) {
            Post("/api/plugin/jobs/" + id + "/result", "FAIL\t" + NormalizePath(paths[0]) + "\thook extraction disabled: crashes TM2020");
            NotifyProblem("PlugResource.Gbx hook extraction is disabled because it crashes TM2020");
            return;
        }
        BeginBatch("Extracting game files for Blender...");
        array<string>@ lines = RunExtract(paths, id, hook);
        Post("/api/plugin/jobs/" + id + "/result", Join(lines, "\n"));
    } else {
        Post("/api/plugin/jobs/" + id + "/result", "FAIL\tGameData/\tunknown job kind");
        NotifyProblem("Rejected unknown job kind: " + kind);
    }
}

string Join(array<string>@ lines, const string &in sep) {
    string text = "";
    for (uint i = 0; i < lines.Length; i++) {
        if (i > 0) text += sep;
        text += lines[i];
    }
    return text;
}

string NormalizePath(const string &in line) {
    string path = line.Trim().Replace("\\", "/");
    while (path.StartsWith("/")) path = path.SubStr(1);
    if (!path.ToLower().StartsWith("gamedata/")) path = "GameData/" + path;
    return path;
}

bool IsFidValid(CSystemFidFile@ fid) {
    if (fid is null) return false;
    bool valid = true;
#if TMNEXT
    string written = fid.TimeWrite;
    valid = written != "?";
#endif
    // ManiaPlanet has no TimeWrite sentinel. Extraction and size checks
    // below remain authoritative for a non-null fid.
    return valid;
}

bool CollectFolder(CSystemFidsFolder@ folder, const string &in prefix, array<CSystemFidFile@>@ fids, array<string>@ paths) {
    for (uint i = 0; i < folder.Leaves.Length; i++) {
        fids.InsertLast(folder.Leaves[i]);
        paths.InsertLast(prefix + folder.Leaves[i].FileName);
    }
    for (uint i = 0; i < folder.Trees.Length; i++) {
        auto child = cast<CSystemFidsFolder>(folder.Trees[i]);
        if (child is null) {
            warn("Cannot enumerate non-folder game node under " + prefix);
            return false;
        }
        if (!CollectFolder(child, prefix + child.DirName + "/", fids, paths)) return false;
    }
    return true;
}

uint64 WrittenSize(const string &in outPath) {
    if (!IO::FileExists(outPath)) return 0;
    return IO::FileSize(outPath);
}

// Returns one result line for one file.
string ExtractOne(CSystemFidFile@ fid, const string &in path, bool hook = false) {
    if (!IsFidValid(fid)) return "NOTFOUND\t" + path + "\tnot in the game files";
    string outPath = IO::FromDataFolder("Extract/" + path);
    if (IO::FileExists(outPath)) IO::Delete(outPath);
    uint64 expected = fid.ByteSize;

    print("Extracting " + path + (hook ? " (hook diagnostic)" : ""));
    bool extracted = Fids::Extract(fid, hook);
    uint64 written = WrittenSize(outPath);
    if (!extracted && written == 0) return "FAIL\t" + path + "\tFids::Extract failed";
    if (expected > 0 && written != expected) {
        return "SIZE\t" + path + "\twrote " + written + " of " + expected + " bytes";
    }
    return "OK\t" + path + "\t" + written;
}

void CountResult(const string &in line) {
    if (line.StartsWith("OK\t")) {
        g_batchOk++;
        return;
    }
    g_batchFailed++;
    warn("Extract " + line.Replace("\t", " "));
    if (g_batchFirstFailure.Length == 0) {
        array<string>@ parts = line.Split("\t");
        g_batchFirstFailure = parts.Length >= 3 ? parts[1] + ": " + parts[2] : line;
    }
}

array<string>@ RunExtract(array<string>@ requests, const string &in id = "", bool hook = false) {
    array<CSystemFidFile@> fids;
    array<string> paths;
    array<string> lines;
    for (uint i = 0; i < requests.Length; i++) {
        string request = requests[i].Trim();
        if (request.Length == 0) continue;
        bool isFolder = request.EndsWith("/") || request.EndsWith("\\");
        string path = NormalizePath(request);
        if (isFolder) {
            while (path.EndsWith("/")) path = path.SubStr(0, path.Length - 1);
            CSystemFidsFolder@ folder = Fids::GetGameFolder(path);
            uint before = fids.Length;
            if (folder !is null && !CollectFolder(folder, path + "/", fids, paths)) {
                fids.Resize(before);
                paths.Resize(before);
                lines.InsertLast("FAIL\t" + path + "/\tcannot enumerate a child folder; see Openplanet log");
                continue;
            }
            if (fids.Length == before) lines.InsertLast("NOTFOUND\t" + path + "/\tfolder not found or empty");
        } else {
            fids.InsertLast(Fids::GetGame(path));
            paths.InsertLast(path);
        }
    }
    for (uint i = 0; i < lines.Length; i++) CountResult(lines[i]);
    for (uint i = 0; i < fids.Length; i++) {
        Heartbeat(id, i, fids.Length, paths[i]);
        string result = ExtractOne(fids[i], paths[i], hook);
        CountResult(result);
        lines.InsertLast(result);
        yield();
    }
    return lines;
}

uint g_indexTotal = 0;

bool IndexFolder(CSystemFidsFolder@ folder, const string &in prefix, array<string>@ chunk, const string &in id) {
    for (uint i = 0; i < folder.Leaves.Length; i++) {
        CSystemFidFile@ fid = folder.Leaves[i];
        chunk.InsertLast(prefix + fid.FileName + "\t" + fid.ByteSize);
        if (chunk.Length % 500 == 0) Heartbeat(id, g_indexTotal + chunk.Length, 0, "indexing");
        if (chunk.Length >= INDEX_CHUNK) {
            g_indexTotal += chunk.Length;
            Post("/api/plugin/jobs/" + id + "/index", Join(chunk, "\n"));
            chunk.Resize(0);
        }
    }
    for (uint i = 0; i < folder.Trees.Length; i++) {
        auto child = cast<CSystemFidsFolder>(folder.Trees[i]);
        if (child is null) {
            warn("Cannot index non-folder game node under " + prefix);
            return false;
        }
        if (!IndexFolder(child, prefix + child.DirName + "/", chunk, id)) return false;
    }
    return true;
}

void RunIndex(const string &in id) {
    CSystemFidsFolder@ root = Fids::GetGameFolder("GameData");
    if (root is null) {
        Post("/api/plugin/jobs/" + id + "/result", "FAIL\tGameData/\tgame folder not found");
        NotifyProblem("Indexing failed: GameData folder not found");
        return;
    }
    array<string> chunk;
    g_indexTotal = 0;
    if (!IndexFolder(root, "GameData/", chunk, id)) {
        Post("/api/plugin/jobs/" + id + "/result", "FAIL\tGameData/\tcannot enumerate a child folder; see Openplanet log");
        NotifyProblem("Indexing failed: cannot enumerate a child folder");
        return;
    }
    uint total = g_indexTotal;
    if (chunk.Length > 0) {
        total += chunk.Length;
        Post("/api/plugin/jobs/" + id + "/index", Join(chunk, "\n"));
    }
    Post("/api/plugin/jobs/" + id + "/result", "OK\tGameData/\t" + total);
    g_batchIndex = "Indexed " + total + " game files";
}
