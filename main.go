package main

import (
	"encoding/json"
	"fmt"
	"sync"

	"github.com/router-for-me/CLIProxyAPI/v7/sdk/pluginabi"
	"github.com/router-for-me/CLIProxyAPI/v7/sdk/pluginapi"
)

const (
	pluginName    = "cpa-quota-warmup"
	pluginVersion = "0.6.4"
	logPrefix     = "[cpa-quota-warmup] "
)

func main() { fmt.Println(pluginName, pluginVersion) }

// engineMu guards the single process-wide *engine instance across
// plugin.register / plugin.reconfigure / plugin.shutdown calls.
var engineMu sync.Mutex
var currentEngine *engine

func ensureEngineRunning(cfg pluginConfig) error {
	engineMu.Lock()
	defer engineMu.Unlock()
	if currentEngine != nil {
		currentEngine.settings.Store(&cfg)
		return nil
	}
	path, err := stateFilePath()
	if err != nil {
		return err
	}
	overridesPath, err := overridesFilePath()
	if err != nil {
		return err
	}
	e := newEngine(cfg, newStateStore(path), newOverridesStore(overridesPath), hostAuthLister{})

	if !cfg.legacyMode && !cfg.v3InlineMode {
		// v0.4.0 file mode: wire up the externally maintained
		// quota-warmup.yaml and, best-effort, migrate a stale v0.3.0
		// overrides.json into it (see migrateOverridesToWarmupFile's doc
		// comment). Neither step may block startup: a transient
		// host.auth.list failure here just means the first tick (30s later)
		// generates/reconciles the file instead.
		warmupPath, pathErr := resolveWarmupFilePath(cfg)
		if pathErr != nil {
			return pathErr
		}
		e.warmupFile = newWarmupFileManager(warmupPath)
		l := logLanguage(cfg)
		if entries, listErr := e.auths.ListAuths(); listErr == nil {
			if migrated, migErr := migrateOverridesToWarmupFile(overridesPath, e.warmupFile, entries); migErr != nil {
				hostLog("warn", tr(l, msgOverridesMigrateFailed, warmupPath, migErr))
			} else if migrated {
				hostLog("info", tr(l, msgOverridesMigrated, warmupPath))
			}
			if err := e.warmupFile.ensureFresh(entries); err != nil {
				hostLog("warn", tr(l, msgWarmupFileError, warmupPath, err))
			}
		}
	}

	e.start()
	currentEngine = e
	return nil
}

func reconfigureEngine(cfg pluginConfig) error {
	engineMu.Lock()
	e := currentEngine
	engineMu.Unlock()
	if e == nil {
		// No prior register somehow reached us; behave like a first
		// register instead of losing the config.
		return ensureEngineRunning(cfg)
	}
	e.settings.Store(&cfg)
	return nil
}

func shutdownEngine() {
	engineMu.Lock()
	e := currentEngine
	currentEngine = nil
	engineMu.Unlock()
	if e != nil {
		e.stop()
	}
}

func activeEngine() *engine {
	engineMu.Lock()
	defer engineMu.Unlock()
	return currentEngine
}

func handleMethod(method string, raw []byte) ([]byte, error) {
	switch method {
	case pluginabi.MethodPluginRegister:
		cfg, err := decodeConfig(raw)
		if err != nil {
			return nil, err
		}
		if cfg.legacyMode || cfg.v3InlineMode {
			warmupPath, pathErr := resolveWarmupFilePath(cfg)
			if pathErr == nil {
				hostLog("info", tr(logLanguage(cfg), msgInlineModeNotice, warmupPath))
			}
		}
		if err := ensureEngineRunning(cfg); err != nil {
			return nil, err
		}
		return okEnvelope(registrationPayload())
	case pluginabi.MethodPluginReconfigure:
		cfg, err := decodeConfig(raw)
		if err != nil {
			return nil, err
		}
		if err := reconfigureEngine(cfg); err != nil {
			return nil, err
		}
		return okEnvelope(registrationPayload())
	case pluginabi.MethodPluginQuiesce:
		return okEnvelope(struct{}{})
	case pluginabi.MethodPluginShutdown:
		shutdownEngine()
		return okEnvelope(struct{}{})
	case pluginabi.MethodUsageHandle:
		var record pluginapi.UsageRecord
		if len(raw) > 0 {
			_ = json.Unmarshal(raw, &record)
		}
		if e := activeEngine(); e != nil {
			e.ring.record(record)
		}
		return okEnvelope(struct{}{})
	case pluginabi.MethodManagementRegister:
		return okEnvelope(managementRegistration())
	case pluginabi.MethodManagementHandle:
		return handleManagementRequest(raw)
	default:
		return errorEnvelope("unknown_method", "unsupported method"), nil
	}
}

func registrationPayload() any {
	return struct {
		SchemaVersion uint32             `json:"schema_version"`
		Metadata      pluginapi.Metadata `json:"metadata"`
		Capabilities  map[string]bool    `json:"capabilities"`
	}{
		SchemaVersion: pluginabi.SchemaVersion,
		Metadata: pluginapi.Metadata{
			Name:    pluginName,
			Version: pluginVersion,
			Author:  "szxypi",
			Logo:    logoDataURI,
			// Required by CPA. This points to the host SDK, not a published plugin repository.
			GitHubRepository: "https://github.com/router-for-me/CLIProxyAPI",
			// Descriptions are static strings fixed at plugin.register time, so
			// they cannot follow a visiting browser's language the way host.log
			// lines and the status/run JSON do (see i18n.go). They used to be
			// one bilingual sentence (中文 / English) each; v0.5.0 switched
			// them to Chinese-only per the coordinator's explicit instruction
			// (this operator-facing config surface is Chinese-first, unlike
			// the fully-localized status/run JSON and panel page).
			//
			// v0.4.0 reduced this list further to just 3 entries: even
			// per-account schedule/model configuration is no longer done in
			// config.yaml at all -- it lives in the externally maintained
			// quota-warmup.yaml (see warmupfile.go), which this plugin
			// generates and keeps in sync with host.auth.list on its own.
			// The v0.3.0 top-level time/model/accounts keys and the legacy
			// (v0.1/v0.2) default/providers/auths/timezone/... keys still
			// parse exactly as before (see config.go's legacyMode/
			// v3InlineMode) but are intentionally no longer advertised here.
			ConfigFields: []pluginapi.ConfigField{
				{Name: "enabled", Type: pluginapi.ConfigFieldTypeBoolean, Description: "为 false 时插件完全不预热任何账号"},
				{Name: "config-file", Type: pluginapi.ConfigFieldTypeString, Description: "账号预热配置文件路径，默认为 CPA 工作目录下的 quota-warmup.yaml；该文件由插件自动生成与维护，每个认证文件一段，直接改这个文件即可（无需重启），也可以在面板上编辑"},
				{Name: "advanced", Type: pluginapi.ConfigFieldTypeObject, Description: "高级设置，一般不用改，全部可选：timezone（默认跟随宿主进程本地时区）、base-url（默认 http://127.0.0.1:8317）、api-key（默认读取宿主 config.yaml 的 api-keys[0]）、message（默认 hi）、max-tokens（默认 16）、max-rounds（默认 3）、catch-up-minutes（默认 60）、language（默认 auto）、log（默认 true）"},
			},
		},
		Capabilities: map[string]bool{"usage_plugin": true, "management_api": true},
	}
}

func okEnvelope(result any) ([]byte, error) {
	return json.Marshal(struct {
		OK     bool `json:"ok"`
		Result any  `json:"result"`
	}{true, result})
}

func errorEnvelope(code, message string) []byte {
	out, _ := json.Marshal(map[string]any{"ok": false, "error": map[string]string{"code": code, "message": message}})
	return out
}
