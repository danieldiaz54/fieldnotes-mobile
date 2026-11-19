/**
 * FieldNotes Mobile Presentation Screen (React Native + Expo + Strict TypeScript).
 *
 * Displays on-device field inspection notes, an interactive Offline/Online connectivity
 * banner, a queued SQLite outbox badge, monotonic Lamport clock telemetry, and an explicit
 * side-by-side Conflict Resolution modal sheet (KEEP_LOCAL / ACCEPT_REMOTE / MANUAL_MERGE).
 */

import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  FlatList,
  Modal,
  Platform,
  Pressable,
  SafeAreaView,
  ScrollView,
  StyleSheet,
  Switch,
  Text,
  TextInput,
  View,
} from "react-native";

import { ConflictResolver, LamportClock } from "../domain/ConflictResolver";
import {
  ConflictRecord,
  ConflictStrategy,
  FieldNote,
  InspectionSeverity,
  OfflineNetworkError,
  PendingMutation,
} from "../domain/FieldNote";
import {
  FieldNotesSyncHttpClient,
  LocalDeviceStorePort,
  SqliteOutboxRepository,
} from "../data/SqliteOutboxRepository";

export interface FieldNotesScreenProps {
  readonly repository?: LocalDeviceStorePort;
  readonly syncApiBaseUrl?: string;
  readonly deviceId?: string;
}

const MONO_FONT = Platform.select({
  ios: "Menlo",
  android: "monospace",
  default: "monospace",
});

const SEVERITY_COLORS: Readonly<Record<InspectionSeverity, string>> = {
  CRITICAL: "#B91C1C",
  WARNING: "#B45309",
  INFO: "#0F766E",
};

export const FieldNotesScreen: React.FC<FieldNotesScreenProps> = ({
  repository,
  syncApiBaseUrl = "http://127.0.0.1:8097",
  deviceId = "device-ios-field-01",
}) => {
  const storeRef = useRef<LocalDeviceStorePort>(
    repository ?? new SqliteOutboxRepository("fieldnotes_device.db", deviceId),
  );
  const clientRef = useRef<FieldNotesSyncHttpClient>(
    new FieldNotesSyncHttpClient(syncApiBaseUrl, deviceId),
  );
  const clockRef = useRef<LamportClock>(new LamportClock(0));

  const [isOnline, setIsOnline] = useState<boolean>(true);
  const [lamportTs, setLamportTs] = useState<number>(0);
  const [notes, setNotes] = useState<readonly FieldNote[]>([]);
  const [queuedMutations, setQueuedMutations] = useState<readonly PendingMutation[]>([]);
  const [conflicts, setConflicts] = useState<readonly ConflictRecord[]>([]);
  const [statusMessage, setStatusMessage] = useState<string>(
    "Local-first SQLite outbox ready.",
  );

  // Conflict resolution sheet state
  const [activeConflict, setActiveConflict] = useState<ConflictRecord | null>(null);
  const [selectedStrategy, setSelectedStrategy] = useState<ConflictStrategy>("MANUAL_MERGE");
  const [mergedTitle, setMergedTitle] = useState<string>("");
  const [mergedBody, setMergedBody] = useState<string>("");
  const [resolutionError, setResolutionError] = useState<string | null>(null);

  const refreshLocalState = useCallback(async (): Promise<void> => {
    const store = storeRef.current;
    const [loadedNotes, loadedQueue, loadedConflicts, maxTs] = await Promise.all([
      store.listLocalNotes(),
      store.listPendingMutations(true),
      store.listConflicts(true),
      store.getMaxLamportTs(),
    ]);
    if (maxTs > clockRef.current.current()) {
      clockRef.current = new LamportClock(maxTs);
    }
    setNotes(loadedNotes);
    setQueuedMutations(loadedQueue);
    setConflicts(loadedConflicts);
    setLamportTs(clockRef.current.current());
  }, []);

  useEffect(() => {
    void (async () => {
      await storeRef.current.initialize();
      await refreshLocalState();
    })();
  }, [refreshLocalState]);

  const handleCaptureOfflineDraft = useCallback(async (): Promise<void> => {
    const nextTs = clockRef.current.tick();
    const noteId = `NOTE-FIELD-${nextTs.toString().padStart(3, "0")}`;
    const draft: FieldNote = {
      note_id: noteId,
      site_code: "SUB-ANDES-01",
      title: `Field Thermal Check #${String(nextTs)}`,
      body: "Captured locally in SQLite outbox; guaranteed durable across signal loss.",
      severity: "WARNING",
      location: { latitude: 4.711, longitude: -74.0721 },
      author_device_id: deviceId,
      version: 1,
      lamport_ts: nextTs,
      sync_state: "PENDING_PUSH",
      updated_at: new Date().toISOString(),
      deleted: false,
    };

    await storeRef.current.upsertLocalNoteOffline(draft, 0, "UPSERT");
    setStatusMessage(
      isOnline
        ? `Saved ${noteId} locally and queued outbox mutation (L:${String(nextTs)}).`
        : `Offline capture saved ${noteId} to SQLite outbox (L:${String(nextTs)}).`,
    );
    await refreshLocalState();
  }, [deviceId, isOnline, refreshLocalState]);

  const handleRunDeltaSync = useCallback(async (): Promise<void> => {
    try {
      const result = await clientRef.current.triggerDeltaSync(isOnline);
      clockRef.current.receive(result.lamport_ts);
      setStatusMessage(
        `Sync complete: pushed=${String(result.pushed_count)}, pulled=${String(result.pulled_count)}, conflicts=${String(result.conflicts_detected)}.`,
      );
      await refreshLocalState();
    } catch (error: unknown) {
      if (error instanceof OfflineNetworkError) {
        setStatusMessage(
          `503 OFFLINE: ${error.message} (${String(queuedMutations.length)} queued).`,
        );
        return;
      }
      setStatusMessage(
        error instanceof Error ? error.message : "Delta sync encountered an error.",
      );
    }
  }, [isOnline, queuedMutations.length, refreshLocalState]);

  const openConflictSheet = useCallback((conflict: ConflictRecord): void => {
    setActiveConflict(conflict);
    setSelectedStrategy("MANUAL_MERGE");
    setMergedTitle(`${conflict.local_note.title} [Merged]`);
    setMergedBody(
      `Local (${conflict.local_note.author_device_id}): ${conflict.local_note.body}\n` +
        `Remote (${conflict.remote_note.author_device_id}): ${conflict.remote_note.body}`,
    );
    setResolutionError(null);
  }, []);

  const handleConfirmConflictResolution = useCallback(async (): Promise<void> => {
    if (activeConflict === null) {
      return;
    }
    try {
      setResolutionError(null);
      const resolvedLocal = ConflictResolver.resolve(
        activeConflict.local_note,
        activeConflict.remote_note,
        selectedStrategy,
        selectedStrategy === "MANUAL_MERGE"
          ? { mergedTitle, mergedBody }
          : {},
      );

      if (!isOnline) {
        throw new OfflineNetworkError(
          "Reconnect device before committing conflict resolution to remote authority.",
          deviceId,
        );
      }

      const apiResponse = await clientRef.current.resolveConflict(
        activeConflict.conflict_id,
        selectedStrategy === "MANUAL_MERGE"
          ? {
              strategy: selectedStrategy,
              merged_title: mergedTitle.trim(),
              merged_body: mergedBody.trim(),
            }
          : { strategy: selectedStrategy },
      );

      await storeRef.current.markConflictResolved(
        activeConflict.conflict_id,
        selectedStrategy,
        apiResponse.resolved_note ?? resolvedLocal,
      );
      clockRef.current.receive(apiResponse.resolved_note.lamport_ts);
      setActiveConflict(null);
      setStatusMessage(
        `Resolved ${activeConflict.note_id} via ${selectedStrategy} (v${String(apiResponse.resolved_note.version)}).`,
      );
      await refreshLocalState();
    } catch (error: unknown) {
      setResolutionError(
        error instanceof Error ? error.message : "Unable to resolve conflict.",
      );
    }
  }, [
    activeConflict,
    deviceId,
    isOnline,
    mergedBody,
    mergedTitle,
    refreshLocalState,
    selectedStrategy,
  ]);

  const queuedCount = queuedMutations.length;
  const unresolvedConflictCount = conflicts.length;

  const headerSubtitle = useMemo(
    () =>
      `DEVICE ${deviceId}  •  LAMPORT L:${String(lamportTs)}  •  QUEUE ${String(queuedCount)}`,
    [deviceId, lamportTs, queuedCount],
  );

  return (
    <SafeAreaView style={styles.safeArea}>
      {/* Offline / Online Connectivity Banner */}
      <View
        style={[
          styles.connectivityBanner,
          isOnline ? styles.bannerOnline : styles.bannerOffline,
        ]}
      >
        <View style={styles.bannerLeft}>
          <Text style={styles.bannerTitle}>
            {isOnline ? "ONLINE — DELTA SYNC READY" : "OFFLINE — LOCAL SQLITE OUTBOX ACTIVE"}
          </Text>
          <Text style={styles.monoMeta}>{headerSubtitle}</Text>
        </View>
        <Switch value={isOnline} onValueChange={setIsOnline} />
      </View>

      {/* Action Strip & Queued Mutation Badge */}
      <View style={styles.actionRow}>
        <Pressable style={styles.primaryButton} onPress={() => void handleCaptureOfflineDraft()}>
          <Text style={styles.primaryButtonText}>+ Capture Local Note</Text>
        </Pressable>

        <Pressable style={styles.syncButton} onPress={() => void handleRunDeltaSync()}>
          <Text style={styles.syncButtonText}>Run Delta Sync</Text>
          <View style={styles.queueBadge}>
            <Text style={styles.queueBadgeText}>{String(queuedCount)}</Text>
          </View>
        </Pressable>
      </View>

      <View style={styles.statusBar}>
        <Text style={styles.statusText}>{statusMessage}</Text>
      </View>

      {/* Active Conflict Alert Strip */}
      {unresolvedConflictCount > 0 ? (
        <View style={styles.conflictStrip}>
          <Text style={styles.conflictStripTitle}>
            {String(unresolvedConflictCount)} Active Optimistic Sync Conflict(s)
          </Text>
          {conflicts.map((conflict) => (
            <Pressable
              key={conflict.conflict_id}
              style={styles.conflictItemButton}
              onPress={() => openConflictSheet(conflict)}
            >
              <Text style={styles.conflictItemText}>
                Resolve {conflict.note_id} (Local v{String(conflict.local_note.version)} vs Remote v
                {String(conflict.remote_note.version)})
              </Text>
            </Pressable>
          ))}
        </View>
      ) : null}

      {/* On-Device Field Notes List */}
      <FlatList
        data={notes}
        keyExtractor={(item) => item.note_id}
        contentContainerStyle={styles.listContent}
        renderItem={({ item }) => (
          <View style={styles.noteCard}>
            <View style={styles.noteHeaderRow}>
              <Text style={styles.monoCode}>
                {item.note_id} • {item.site_code}
              </Text>
              <View
                style={[
                  styles.severityPill,
                  { backgroundColor: SEVERITY_COLORS[item.severity] },
                ]}
              >
                <Text style={styles.severityPillText}>{item.severity}</Text>
              </View>
            </View>
            <Text style={styles.noteTitle}>{item.title}</Text>
            <Text style={styles.noteBody}>{item.body}</Text>
            <View style={styles.noteFooterRow}>
              <Text style={styles.monoFooter}>
                v{String(item.version)} • L:{String(item.lamport_ts)} •{" "}
                {item.location.latitude.toFixed(4)}, {item.location.longitude.toFixed(4)}
              </Text>
              <Text
                style={[
                  styles.syncStateBadge,
                  item.sync_state === "CONFLICT"
                    ? styles.stateConflict
                    : item.sync_state === "PENDING_PUSH"
                      ? styles.statePending
                      : styles.stateSynced,
                ]}
              >
                {item.sync_state}
              </Text>
            </View>
          </View>
        )}
      />

      {/* Explicit Deterministic Conflict Resolution Sheet */}
      <Modal
        visible={activeConflict !== null}
        animationType="slide"
        transparent
        onRequestClose={() => setActiveConflict(null)}
      >
        <View style={styles.modalBackdrop}>
          <View style={styles.modalSheet}>
            <ScrollView>
              <Text style={styles.sheetTitle}>
                Conflict Resolution • {activeConflict?.note_id ?? ""}
              </Text>
              {activeConflict !== null ? (
                <>
                  <View style={styles.diffBox}>
                    <Text style={styles.diffHeading}>
                      LOCAL (v{String(activeConflict.local_note.version)} • L:
                      {String(activeConflict.local_note.lamport_ts)})
                    </Text>
                    <Text style={styles.diffTitle}>{activeConflict.local_note.title}</Text>
                    <Text style={styles.diffBody}>{activeConflict.local_note.body}</Text>
                  </View>

                  <View style={styles.diffBox}>
                    <Text style={styles.diffHeading}>
                      REMOTE (v{String(activeConflict.remote_note.version)} • L:
                      {String(activeConflict.remote_note.lamport_ts)})
                    </Text>
                    <Text style={styles.diffTitle}>{activeConflict.remote_note.title}</Text>
                    <Text style={styles.diffBody}>{activeConflict.remote_note.body}</Text>
                  </View>

                  <View style={styles.strategyRow}>
                    {(["KEEP_LOCAL", "ACCEPT_REMOTE", "MANUAL_MERGE"] as const).map((strategy) => (
                      <Pressable
                        key={strategy}
                        style={[
                          styles.strategyChip,
                          selectedStrategy === strategy ? styles.strategyChipActive : null,
                        ]}
                        onPress={() => setSelectedStrategy(strategy)}
                      >
                        <Text
                          style={[
                            styles.strategyChipText,
                            selectedStrategy === strategy ? styles.strategyChipTextActive : null,
                          ]}
                        >
                          {strategy}
                        </Text>
                      </Pressable>
                    ))}
                  </View>

                  {selectedStrategy === "MANUAL_MERGE" ? (
                    <View style={styles.mergeInputs}>
                      <Text style={styles.inputLabel}>Merged Title</Text>
                      <TextInput
                        style={styles.textInput}
                        value={mergedTitle}
                        onChangeText={setMergedTitle}
                        placeholder="Enter converged inspection title"
                      />
                      <Text style={styles.inputLabel}>Merged Body</Text>
                      <TextInput
                        style={[styles.textInput, styles.multilineInput]}
                        value={mergedBody}
                        onChangeText={setMergedBody}
                        multiline
                        placeholder="Enter converged inspection report"
                      />
                    </View>
                  ) : null}

                  {resolutionError !== null ? (
                    <Text style={styles.errorText}>{resolutionError}</Text>
                  ) : null}

                  <View style={styles.sheetActions}>
                    <Pressable
                      style={styles.cancelButton}
                      onPress={() => setActiveConflict(null)}
                    >
                      <Text style={styles.cancelButtonText}>Cancel</Text>
                    </Pressable>
                    <Pressable
                      style={styles.confirmButton}
                      onPress={() => void handleConfirmConflictResolution()}
                    >
                      <Text style={styles.confirmButtonText}>Commit Resolution</Text>
                    </Pressable>
                  </View>
                </>
              ) : null}
            </ScrollView>
          </View>
        </View>
      </Modal>
    </SafeAreaView>
  );
};

const styles = StyleSheet.create({
  safeArea: {
    flex: 1,
    backgroundColor: "#F6F5F2",
  },
  connectivityBanner: {
    paddingHorizontal: 16,
    paddingVertical: 12,
    flexDirection: "row",
    alignItems: "center",
    justifyContent: "space-between",
  },
  bannerOnline: {
    backgroundColor: "#14532D",
  },
  bannerOffline: {
    backgroundColor: "#9A3412",
  },
  bannerLeft: {
    flex: 1,
    paddingRight: 12,
  },
  bannerTitle: {
    color: "#FFFFFF",
    fontSize: 13,
    fontWeight: "700",
    letterSpacing: 0.4,
  },
  monoMeta: {
    color: "#E5E7EB",
    fontFamily: MONO_FONT,
    fontSize: 11,
    marginTop: 4,
  },
  actionRow: {
    flexDirection: "row",
    gap: 10,
    paddingHorizontal: 16,
    paddingTop: 12,
  },
  primaryButton: {
    flex: 1,
    backgroundColor: "#111827",
    paddingVertical: 12,
    paddingHorizontal: 14,
    borderRadius: 8,
    alignItems: "center",
  },
  primaryButtonText: {
    color: "#FFFFFF",
    fontWeight: "600",
    fontSize: 13,
  },
  syncButton: {
    flexDirection: "row",
    alignItems: "center",
    gap: 8,
    backgroundColor: "#0F766E",
    paddingVertical: 12,
    paddingHorizontal: 14,
    borderRadius: 8,
  },
  syncButtonText: {
    color: "#FFFFFF",
    fontWeight: "600",
    fontSize: 13,
  },
  queueBadge: {
    backgroundColor: "#F59E0B",
    minWidth: 22,
    height: 22,
    borderRadius: 11,
    alignItems: "center",
    justifyContent: "center",
    paddingHorizontal: 6,
  },
  queueBadgeText: {
    color: "#111827",
    fontFamily: MONO_FONT,
    fontSize: 11,
    fontWeight: "700",
  },
  statusBar: {
    paddingHorizontal: 16,
    paddingVertical: 8,
  },
  statusText: {
    fontFamily: MONO_FONT,
    fontSize: 11,
    color: "#374151",
  },
  conflictStrip: {
    marginHorizontal: 16,
    marginBottom: 8,
    padding: 12,
    borderRadius: 8,
    backgroundColor: "#FEF2F2",
    borderWidth: 1,
    borderColor: "#FCA5A5",
  },
  conflictStripTitle: {
    color: "#991B1B",
    fontWeight: "700",
    fontSize: 13,
    marginBottom: 8,
  },
  conflictItemButton: {
    backgroundColor: "#B91C1C",
    paddingVertical: 8,
    paddingHorizontal: 10,
    borderRadius: 6,
    marginTop: 4,
  },
  conflictItemText: {
    color: "#FFFFFF",
    fontFamily: MONO_FONT,
    fontSize: 11,
    fontWeight: "600",
  },
  listContent: {
    padding: 16,
    gap: 12,
  },
  noteCard: {
    backgroundColor: "#FFFFFF",
    borderRadius: 10,
    padding: 14,
    borderWidth: 1,
    borderColor: "#E5E7EB",
    marginBottom: 10,
  },
  noteHeaderRow: {
    flexDirection: "row",
    justifyContent: "space-between",
    alignItems: "center",
    marginBottom: 6,
  },
  monoCode: {
    fontFamily: MONO_FONT,
    fontSize: 11,
    color: "#4B5563",
  },
  severityPill: {
    borderRadius: 4,
    paddingHorizontal: 8,
    paddingVertical: 2,
  },
  severityPillText: {
    color: "#FFFFFF",
    fontFamily: MONO_FONT,
    fontSize: 10,
    fontWeight: "700",
  },
  noteTitle: {
    fontSize: 15,
    fontWeight: "700",
    color: "#111827",
    marginBottom: 4,
  },
  noteBody: {
    fontSize: 13,
    color: "#374151",
    lineHeight: 18,
  },
  noteFooterRow: {
    marginTop: 10,
    flexDirection: "row",
    justifyContent: "space-between",
    alignItems: "center",
  },
  monoFooter: {
    fontFamily: MONO_FONT,
    fontSize: 11,
    color: "#6B7280",
  },
  syncStateBadge: {
    fontFamily: MONO_FONT,
    fontSize: 10,
    fontWeight: "700",
    paddingHorizontal: 8,
    paddingVertical: 3,
    borderRadius: 4,
  },
  stateSynced: {
    backgroundColor: "#DCFCE7",
    color: "#166534",
  },
  statePending: {
    backgroundColor: "#FEF3C7",
    color: "#92400E",
  },
  stateConflict: {
    backgroundColor: "#FEE2E2",
    color: "#991B1B",
  },
  modalBackdrop: {
    flex: 1,
    backgroundColor: "rgba(17, 24, 39, 0.55)",
    justifyContent: "flex-end",
  },
  modalSheet: {
    backgroundColor: "#FFFFFF",
    borderTopLeftRadius: 16,
    borderTopRightRadius: 16,
    padding: 18,
    maxHeight: "88%",
  },
  sheetTitle: {
    fontSize: 16,
    fontWeight: "700",
    color: "#111827",
    marginBottom: 12,
  },
  diffBox: {
    backgroundColor: "#F9FAFB",
    borderWidth: 1,
    borderColor: "#E5E7EB",
    borderRadius: 8,
    padding: 10,
    marginBottom: 10,
  },
  diffHeading: {
    fontFamily: MONO_FONT,
    fontSize: 11,
    fontWeight: "700",
    color: "#374151",
    marginBottom: 4,
  },
  diffTitle: {
    fontSize: 13,
    fontWeight: "700",
    color: "#111827",
  },
  diffBody: {
    fontSize: 12,
    color: "#4B5563",
    marginTop: 2,
  },
  strategyRow: {
    flexDirection: "row",
    gap: 8,
    marginVertical: 10,
  },
  strategyChip: {
    flex: 1,
    paddingVertical: 8,
    borderRadius: 6,
    borderWidth: 1,
    borderColor: "#D1D5DB",
    alignItems: "center",
  },
  strategyChipActive: {
    backgroundColor: "#111827",
    borderColor: "#111827",
  },
  strategyChipText: {
    fontFamily: MONO_FONT,
    fontSize: 10,
    fontWeight: "700",
    color: "#374151",
  },
  strategyChipTextActive: {
    color: "#FFFFFF",
  },
  mergeInputs: {
    marginTop: 4,
  },
  inputLabel: {
    fontSize: 12,
    fontWeight: "600",
    color: "#374151",
    marginBottom: 4,
    marginTop: 8,
  },
  textInput: {
    borderWidth: 1,
    borderColor: "#D1D5DB",
    borderRadius: 6,
    paddingHorizontal: 10,
    paddingVertical: 8,
    fontSize: 13,
    color: "#111827",
  },
  multilineInput: {
    minHeight: 84,
    textAlignVertical: "top",
  },
  errorText: {
    color: "#B91C1C",
    fontSize: 12,
    marginTop: 8,
    fontWeight: "600",
  },
  sheetActions: {
    flexDirection: "row",
    gap: 10,
    marginTop: 16,
  },
  cancelButton: {
    flex: 1,
    paddingVertical: 12,
    borderRadius: 8,
    borderWidth: 1,
    borderColor: "#D1D5DB",
    alignItems: "center",
  },
  cancelButtonText: {
    color: "#374151",
    fontWeight: "600",
  },
  confirmButton: {
    flex: 1,
    paddingVertical: 12,
    borderRadius: 8,
    backgroundColor: "#0F766E",
    alignItems: "center",
  },
  confirmButtonText: {
    color: "#FFFFFF",
    fontWeight: "700",
  },
});

export default FieldNotesScreen;
