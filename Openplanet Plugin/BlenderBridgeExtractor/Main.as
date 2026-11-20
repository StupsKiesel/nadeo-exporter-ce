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
// "path<TAB>size[<TAB>nodata]" lines ("nodata": listed by the game without
// any data, never extractable). Results go to POST /api/plugin/jobs/<id>/result
// as "STATUS<TAB>path<TAB>detail[<TAB>reason]" lines: STATUS is OK, SIZE, FAIL
// or NOTFOUND; the OK detail is the written size; reason is one of missing,
// nodata, truncated, oversized, extract-false, timeout, unsized, rejected.
// While a job runs, POST /api/plugin/jobs/<id>/progress
// ("done<TAB>total<TAB>message") tells Blender the plugin is still alive; a
// 404 answer means Blender cancelled or forgot the job, which stops it.
//
// The overlay's menu bar shows a status-tinted Blender logo; clicking it opens
// a fixed panel at the top right with the polling switch and quick settings.
// Jobs that arrive back to back (one map import) share one summary.

[Setting category="General" name="Listen for Blender requests"]
bool S_Enabled = true;

[Setting category="General" name="Show in the overlay menu bar"]
bool S_ShowInMenuBar = true;

[Setting category="General" name="Blender bridge URL"]
string S_ServerUrl = "http://127.0.0.1:1337";

[Setting category="General" name="Log every extracted file" description="Failures are always logged."]
bool S_LogFiles = true;

const string PLUGIN_VERSION = "2.2.2";
const string TITLE = "Blender Bridge (Trackmania 2020)";
const uint INDEX_CHUNK = 5000;
// Extract files back to back for up to this many ms before yielding a frame.
const uint64 FRAME_BUDGET = 8;
// Seconds to wait for a follow-up job before a batch counts as finished.
const int BATCH_GAP = 3;
// How long the menu bar logo stays red after a batch with failures.
const uint64 FAILURE_ICON_TIME = 30000;
// Basenames, lowercase. Mirror GBXBPY/library/server.py's HOOK_BLACKLIST.
const array<string> HOOK_BLACKLIST = {"plugresource.gbx"};

bool IsHookBlacklisted(const string &in path) {
    string key = NormalizePath(path).ToLower();
    for (uint i = 0; i < HOOK_BLACKLIST.Length; i++) {
        if (key == HOOK_BLACKLIST[i] || key.EndsWith("/" + HOOK_BLACKLIST[i])) return true;
    }
    return false;
}

bool g_connected = false;
Net::HttpRequest@ g_ping;
string g_pingJob = "";
uint64 g_lastPing = 0;
// Job Blender answered 404 for: cancelled there, or the bridge restarted.
string g_cancelledJob = "";

// Current batch of back-to-back jobs.
bool g_inBatch = false;
uint g_batchOk = 0;
uint g_batchFailed = 0;
string g_batchFirstFailure = "";
string g_batchIndex = "";

// Menu state.
bool g_panelOpen = false;
float g_barRight = 0;
float g_barBottom = 0;
const float PANEL_WIDTH = 340;
UI::Texture@ g_logo;
bool g_logoLoaded = false;
string g_jobKind = "";
uint g_jobDone = 0;
uint g_jobTotal = 0;
string g_jobFile = "";
string g_lastSummary = "";
string g_lastFailure = "";
uint64 g_lastFailureAt = 0;

// Logs a rejected or failed job and shows it in the panel.
void ReportProblem(const string &in text) {
    warn(text);
    g_lastSummary = text;
    g_lastFailure = "";
}

string ServerUrl() {
    string url = S_ServerUrl.Trim();
    while (url.EndsWith("/")) url = url.SubStr(0, url.Length - 1);
    return url;
}

string GameVersion() {
    string version = "";
    auto app = GetApp();
    if (app !is null && app.SystemPlatform !is null) version = app.SystemPlatform.ExeVersion;
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
    CheckPing();
    if (Time::Now - g_lastPing < 2000) return;
    g_lastPing = Time::Now;
    g_pingJob = id;
    @g_ping = Net::HttpPost(ServerUrl() + "/api/plugin/jobs/" + id + "/progress",
        "" + done + "\t" + total + "\t" + message, "text/plain; charset=utf-8");
}

void CheckPing() {
    if (g_ping is null || !g_ping.Finished()) return;
    if (g_ping.ResponseCode() == 404) g_cancelledJob = g_pingJob;
    @g_ping = null;
}

// True once Blender answered a heartbeat for this job with 404.
bool JobCancelled(const string &in id) {
    if (id.Length == 0) return false;
    CheckPing();
    return g_cancelledJob == id;
}

void Main() {
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
    print((connected ? "Connected to Blender (" : "Blender not reachable (") + ServerUrl() + ")");
}

// Listening switched off: forget the connection quietly.
void Pause() {
    g_connected = false;
}

// The menu bar shows a white Blender silhouette tinted by status: dark grey
// when listening is off, red for a while after a batch had failures.
// Logo tint as 0xRRGGBBAA; `textColor` is the same colour as a format code.
uint LogoColor(string &out textColor) {
    if (!S_Enabled) { textColor = "\\$555"; return 0x555555FF; }
    if (g_jobKind.Length > 0) { textColor = "\\$4af"; return 0x44AAFFFF; }
    if (g_lastFailure.Length > 0 && Time::Now - g_lastFailureAt < FAILURE_ICON_TIME) {
        textColor = "\\$f44";
        return 0xFF4444FF;
    }
    if (g_connected) { textColor = "\\$4f8"; return 0x44FF88FF; }
    textColor = "\\$ccc";
    return 0xCCCCCCFF;
}

string StatusLine() {
    if (!S_Enabled) return "Paused: not listening for Blender";
    if (g_connected) return "\\$4f8" + Icons::Check + "\\$z Connected to Blender";
    return "Waiting for Blender";
}

string JobLine() {
    string count = g_jobTotal > 0 ? " " + g_jobDone + " of " + g_jobTotal : g_jobDone > 0 ? " " + g_jobDone : "";
    return Icons::Download + " " + g_jobKind + count;
}

void RenderMenuMain() {
    if (!S_ShowInMenuBar) return;
    if (!g_logoLoaded) {
        g_logoLoaded = true;
        @g_logo = UI::LoadTexture("blender.png");
    }
    // Draw at the right end of the menu bar, then put the cursor back so
    // plugins drawn after this one keep their place on the left.
    vec2 start = UI::GetCursorPos();
    string plain = "";
    string textColor;
    uint logoColor = LogoColor(textColor);
    float lineHeight = UI::MeasureString("A").y;
    float barHeight = UI::GetWindowSize().y;
    float logoSize = Math::Max(lineHeight, barHeight * 0.8f);
    // Leading spaces reserve room for the logo inside the clickable label.
    string pad = "";
    if (g_logo is null) {
        pad = textColor + "Blender\\$z";
        plain = "Blender";
    } else {
        float space = Math::Max(UI::MeasureString(" ").x, 1.0f);
        while (UI::MeasureString(pad).x < logoSize + space) pad += " ";
        plain = pad;
    }
    float spacing = UI::GetStyleVarVec2(UI::StyleVar::ItemSpacing).x;
    float width = UI::MeasureString(plain).x + spacing;
    float x = Math::Max(start.x, start.x + UI::GetContentRegionAvail().x - width);
    if (g_logo !is null) {
        vec2 pos = UI::GetWindowPos() + vec2(x + spacing * 0.5f, Math::Max(0.0f, (barHeight - logoSize) * 0.5f));
        UI::GetWindowDrawList().AddImage(g_logo, pos, vec2(logoSize, logoSize), logoColor);
    }
    UI::SetCursorPos(vec2(x, start.y));
    if (UI::MenuItem(pad + "###BlenderBridgeMenu", "", g_panelOpen)) g_panelOpen = !g_panelOpen;
    float progress = JobProgress();
    if (progress >= 0) {
        // Thin progress line along the bottom edge of the menu item.
        vec2 base = UI::GetWindowPos() + vec2(x, barHeight - 2);
        auto draw = UI::GetWindowDrawList();
        draw.AddRectFilled(vec4(base, vec2(width, 2)), vec4(1, 1, 1, 0.15f));
        draw.AddRectFilled(vec4(base, vec2(width * progress, 2)), vec4(0.27f, 0.67f, 1, 1));
    }
    if (UI::IsItemHovered() && !g_panelOpen) {
        UI::BeginTooltip();
        UI::Text(TITLE);
        UI::Text(StatusLine());
        if (g_jobKind.Length > 0) UI::Text(JobLine());
        else if (g_lastFailure.Length > 0) UI::Text("\\$f44" + Icons::ExclamationTriangle + "\\$z " + g_lastSummary);
        UI::EndTooltip();
    }
    g_barRight = UI::GetWindowPos().x + UI::GetWindowSize().x;
    g_barBottom = UI::GetWindowPos().y + barHeight;
    UI::SetCursorPos(start);
}

void RenderMenu() {
    if (UI::MenuItem(Icons::Plug + " " + TITLE, "", g_panelOpen)) g_panelOpen = !g_panelOpen;
}

// Fraction of the running job that is done, or -1 when no counted job runs.
float JobProgress() {
    if (g_jobKind.Length == 0 || g_jobTotal == 0) return -1;
    return Math::Min(1.0f, float(g_jobDone) / float(g_jobTotal));
}

// Port of the bridge URL, or 0 when it has none.
int ServerPort() {
    string[]@ parts = UrlHost().Split(":");
    if (parts.Length < 2) return 0;
    return Text::ParseInt(parts[parts.Length - 1]);
}

// Host part of the bridge URL (without scheme and path).
string UrlHost() {
    string url = ServerUrl();
    int scheme = url.IndexOf("://");
    if (scheme >= 0) url = url.SubStr(scheme + 3);
    int slash = url.IndexOf("/");
    if (slash >= 0) url = url.SubStr(0, slash);
    return url;
}

void SetServerPort(int port) {
    string host = UrlHost().Split(":")[0];
    if (host.Length == 0) host = "127.0.0.1";
    S_ServerUrl = "http://" + host + ":" + port;
}

// Fixed panel below the right end of the overlay menu bar.
void RenderInterface() {
    if (!g_panelOpen) return;
    float scale = UI::GetScale();
    float right = g_barRight > 0 ? g_barRight : float(Display::GetWidth());
    UI::SetNextWindowSize(int(PANEL_WIDTH), 0, UI::Cond::Always);
    UI::SetNextWindowPos(int(right / scale - PANEL_WIDTH), int(g_barBottom / scale), UI::Cond::Always);
    auto flags = UI::WindowFlags(UI::WindowFlags::NoMove | UI::WindowFlags::NoResize | UI::WindowFlags::NoCollapse
        | UI::WindowFlags::NoSavedSettings | UI::WindowFlags::NoDocking | UI::WindowFlags::NoTitleBar);
    if (UI::Begin(TITLE + "###BlenderBridgePanel", g_panelOpen, flags)) RenderPanelContent();
    UI::End();
}

void RenderPanelContent() {
    S_Enabled = UI::Checkbox("Listen for Blender requests", S_Enabled);
    UI::TextDisabled(S_Enabled ? "Polls Blender for extraction requests." : "Not polling; Blender cannot request files.");
    UI::Text(StatusLine());
    if (g_jobKind.Length > 0) {
        UI::Text(JobLine());
        float progress = JobProgress();
        if (progress >= 0) UI::ProgressBar(progress, vec2(-1, 4 * UI::GetScale()), "");
        if (g_jobFile.Length > 0) UI::TextDisabled(g_jobFile);
    } else if (g_lastSummary.Length > 0) {
        UI::Text("Last: " + g_lastSummary);
        if (g_lastFailure.Length > 0) UI::TextWrapped("\\$f44" + Icons::ExclamationTriangle + "\\$z " + g_lastFailure);
    }

    UI::SeparatorText("Settings");
    int port = ServerPort();
    UI::SetNextItemWidth(120);
    int newPort = UI::InputInt("Port", port);
    if (newPort != port && newPort > 0 && newPort < 65536) SetServerPort(newPort);
    UI::TextDisabled("Must match the port in Blender's addon preferences.");
    S_LogFiles = UI::Checkbox("Log every extracted file", S_LogFiles);
    S_ShowInMenuBar = UI::Checkbox("Show in the menu bar", S_ShowInMenuBar);

    UI::Separator();
    if (UI::Button(Icons::FolderOpen + " Extract folder")) {
        string folder = IO::FromDataFolder("Extract");
        if (!IO::FolderExists(folder)) IO::CreateFolder(folder);
        OpenExplorerPath(folder);
    }
    UI::SameLine();
    if (UI::Button(Icons::Clipboard + " Copy log path")) IO::SetClipboard(IO::FromDataFolder("Openplanet.log"));
    UI::TextDisabled("Plugin v" + PLUGIN_VERSION);
}

void BeginBatch() {
    if (g_inBatch) return;
    g_inBatch = true;
    g_batchOk = 0;
    g_batchFailed = 0;
    g_batchFirstFailure = "";
    g_batchIndex = "";
}

void EndBatch() {
    if (!g_inBatch) return;
    g_inBatch = false;
    if (g_batchIndex.Length > 0) {
        print(g_batchIndex);
        g_lastSummary = g_batchIndex;
        g_lastFailure = "";
    }
    uint total = g_batchOk + g_batchFailed;
    if (total == 0) return;
    g_lastSummary = "Extracted " + g_batchOk + " of " + total + (total == 1 ? " file" : " files");
    g_lastFailure = g_batchFirstFailure;
    g_lastFailureAt = Time::Now;
    if (g_batchFailed > 0) {
        warn("Extracted " + g_batchOk + " of " + total + " files, " + g_batchFailed + " failed; first: " + g_batchFirstFailure);
    } else {
        print("Extracted " + total + (total == 1 ? " file" : " files") + " for Blender");
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
    g_cancelledJob = "";
    g_jobKind = kind == "index" ? "Indexing" : kind == "extract" ? "Extracting" : "Hook extracting";
    g_jobDone = 0;
    g_jobTotal = 0;
    g_jobFile = "";
    RunJobKind(id, kind, paths);
    g_jobKind = "";
    g_jobTotal = 0;
    g_jobFile = "";
}

void RunJobKind(const string &in id, const string &in kind, array<string>@ paths) {
    if (kind == "index") {
        BeginBatch();
        RunIndex(id);
    } else if (kind == "extract" || kind == "extract_hook_tree" || kind == "extract_hook") {
        bool hook = kind != "extract";
        string suffix = kind == "extract_hook_tree" ? ".vegettreemodel.gbx" : ".gbx";
        if (hook && (paths.Length != 1 || !paths[0].ToLower().EndsWith(suffix))) {
            Post("/api/plugin/jobs/" + id + "/result", "FAIL\tGameData/\thook diagnostic requires exactly one matching GBX file\trejected");
            ReportProblem("Rejected unsafe hook diagnostic request");
            return;
        }
        if (hook && IsHookBlacklisted(paths[0])) {
            Post("/api/plugin/jobs/" + id + "/result", "FAIL\t" + NormalizePath(paths[0]) + "\thook extraction disabled: crashes TM2020\trejected");
            ReportProblem(NormalizePath(paths[0]) + ": hook extraction is disabled because it crashes TM2020");
            return;
        }
        BeginBatch();
        array<string>@ lines = RunExtract(paths, id, hook);
        if (JobCancelled(id)) {
            print("Blender cancelled job " + id + " after " + lines.Length + " files");
            return;
        }
        Post("/api/plugin/jobs/" + id + "/result", Join(lines, "\n"));
    } else {
        Post("/api/plugin/jobs/" + id + "/result", "FAIL\tGameData/\tunknown job kind\trejected");
        ReportProblem("Rejected unknown job kind: " + kind);
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
    string written = fid.TimeWrite;
    return written != "?";
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
    if (fid is null) return "NOTFOUND\t" + path + "\tnot in the game files\tmissing";
    if (!IsFidValid(fid)) return "NOTFOUND\t" + path + "\tlisted by the game without data\tnodata";
    if (S_LogFiles) print("Extracting " + path + (hook ? " (hook diagnostic)" : ""));
    return TryExtract(fid, path, hook);
}

string TryExtract(CSystemFidFile@ fid, const string &in path, bool hook) {
    string outPath = IO::FromDataFolder("Extract/" + path);
    if (IO::FileExists(outPath)) IO::Delete(outPath);
    uint64 expected = fid.ByteSize;
    bool extracted = Fids::Extract(fid, hook);
    uint64 written = WrittenSize(outPath);
    if (!extracted && written == 0) return "FAIL\t" + path + "\tFids::Extract failed\textract-false";
    return WrittenResult(path, expected, written);
}

string WrittenResult(const string &in path, uint64 expected, uint64 written) {
    if (expected > 0 && written != expected) {
        return "SIZE\t" + path + "\twrote " + written + " of " + expected + " bytes\t"
            + (written < expected ? "truncated" : "oversized");
    }
    return "OK\t" + path + "\t" + written + (expected == 0 ? "\tunsized" : "");
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
                lines.InsertLast("FAIL\t" + path + "/\tcannot enumerate a child folder; see Openplanet log\trejected");
                continue;
            }
            if (fids.Length == before) lines.InsertLast("NOTFOUND\t" + path + "/\tfolder not found or empty\tmissing");
        } else {
            fids.InsertLast(Fids::GetGame(path));
            paths.InsertLast(path);
        }
    }
    for (uint i = 0; i < lines.Length; i++) CountResult(lines[i]);
    g_jobTotal = fids.Length;
    ExtractRun run;
    @run.fids = fids;
    @run.paths = paths;
    run.id = id;
    run.hook = hook;
    // Openplanet aborts a coroutine that runs past its script timeout
    // (1 second by default) without yielding, even inside one slow
    // Fids::Extract call (seen on Proton/Wine). Only that coroutine dies, so
    // the files are extracted by a worker; when it dies, the file it was on is
    // judged by what landed on disk and a new worker takes the rest.
    while (run.results.Length < fids.Length && !JobCancelled(id)) {
        await(startnew(ExtractWorker, run));
        uint i = run.results.Length;
        if (i >= fids.Length || JobCancelled(id)) break;
        warn("Extracting " + paths[i] + " exceeded Openplanet's script timeout; continuing with the next file");
        run.results.InsertLast(TimedOutResult(fids[i], paths[i]));
    }
    for (uint i = 0; i < run.results.Length; i++) {
        CountResult(run.results[i]);
        lines.InsertLast(run.results[i]);
    }
    g_jobDone = fids.Length;
    return lines;
}

class ExtractRun {
    array<CSystemFidFile@>@ fids;
    array<string>@ paths;
    // One result line per finished file, in order; its length is the index
    // of the file the worker is on.
    array<string> results;
    string id;
    bool hook = false;
}

void ExtractWorker(ref@ data) {
    ExtractRun@ run = cast<ExtractRun>(data);
    uint64 frameStart = Time::Now;
    while (run.results.Length < run.fids.Length) {
        if (JobCancelled(run.id)) return;
        uint i = run.results.Length;
        g_jobDone = i;
        g_jobFile = run.paths[i];
        Heartbeat(run.id, i, run.fids.Length, run.paths[i]);
        run.results.InsertLast(ExtractOne(run.fids[i], run.paths[i], run.hook));
        if (run.hook || Time::Now - frameStart >= FRAME_BUDGET) {
            yield();
            frameStart = Time::Now;
        }
    }
}

// Result for a file whose worker was aborted. A native call cannot be
// interrupted, and the worker yields before starting a file once its frame
// budget is spent, so the abort came after Fids::Extract returned.
string TimedOutResult(CSystemFidFile@ fid, const string &in path) {
    uint64 written = WrittenSize(IO::FromDataFolder("Extract/" + path));
    if (written == 0) return "FAIL\t" + path + "\tscript timeout while extracting\ttimeout";
    return WrittenResult(path, fid is null ? 0 : fid.ByteSize, written);
}

uint g_indexTotal = 0;

bool IndexFolder(CSystemFidsFolder@ folder, const string &in prefix, array<string>@ chunk, const string &in id) {
    for (uint i = 0; i < folder.Leaves.Length; i++) {
        CSystemFidFile@ fid = folder.Leaves[i];
        chunk.InsertLast(prefix + fid.FileName + "\t" + fid.ByteSize + (IsFidValid(fid) ? "" : "\tnodata"));
        if (chunk.Length % 500 == 0) {
            g_jobDone = g_indexTotal + chunk.Length;
            Heartbeat(id, g_jobDone, 0, "indexing");
        }
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
        ReportProblem("Indexing failed: GameData folder not found");
        return;
    }
    array<string> chunk;
    g_indexTotal = 0;
    if (!IndexFolder(root, "GameData/", chunk, id)) {
        Post("/api/plugin/jobs/" + id + "/result", "FAIL\tGameData/\tcannot enumerate a child folder; see Openplanet log");
        ReportProblem("Indexing failed: cannot enumerate a child folder");
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
