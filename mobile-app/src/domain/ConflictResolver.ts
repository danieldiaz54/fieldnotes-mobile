/**
 * Lamport Logical Clock and Deterministic Optimistic Conflict Resolver.
 *
 * Mirrors `src/domain/sync_engine.py` so mobile clients and backend services
 * enforce identical causal ordering and conflict convergence formulas.
 */

import {
  ConflictStrategy,
  FieldNote,
  InvalidConflictResolutionError,
  InvalidFieldNoteError,
  VALID_CONFLICT_STRATEGIES,
  validateFieldNote,
} from "./FieldNote";

export interface ManualMergeOptions {
  readonly mergedTitle?: string;
  readonly mergedBody?: string;
  readonly updatedAt?: string;
}

export class LamportClock {
  private counter: number;

  public constructor(initial = 0) {
    if (!Number.isInteger(initial) || initial < 0) {
      throw new InvalidFieldNoteError(
        `LamportClock initial value must be an integer >= 0; got ${String(initial)}.`,
      );
    }
    this.counter = initial;
  }

  /** Advance local logical clock by 1 for an on-device mutation event. */
  public tick(): number {
    this.counter += 1;
    return this.counter;
  }

  /** Merge a remote logical timestamp using max(local, remote) + 1. */
  public receive(remoteTs: number): number {
    if (!Number.isInteger(remoteTs) || remoteTs < 0) {
      throw new InvalidFieldNoteError(
        `LamportClock remoteTs must be an integer >= 0; got ${String(remoteTs)}.`,
      );
    }
    this.counter = Math.max(this.counter, remoteTs) + 1;
    return this.counter;
  }

  /** Read the current logical clock value without incrementing. */
  public current(): number {
    return this.counter;
  }
}

export class ConflictResolver {
  /** Return true when local baseVersion diverges from authoritative serverVersion. */
  public static hasVersionConflict(baseVersion: number, serverVersion: number): boolean {
    if (!Number.isInteger(baseVersion) || baseVersion < 0) {
      throw new InvalidFieldNoteError(
        `baseVersion must be an integer >= 0; got ${String(baseVersion)}.`,
      );
    }
    if (!Number.isInteger(serverVersion) || serverVersion < 0) {
      throw new InvalidFieldNoteError(
        `serverVersion must be an integer >= 0; got ${String(serverVersion)}.`,
      );
    }
    return baseVersion !== serverVersion;
  }

  /**
   * Deterministically resolve a conflict between localNote and remoteNote.
   *
   * Advances both version and lamport_ts strictly beyond both conflicting lineages:
   * - nextVersion = max(localNote.version, remoteNote.version) + 1
   * - nextLamport = max(localNote.lamport_ts, remoteNote.lamport_ts) + 1
   */
  public static resolve(
    localNote: FieldNote,
    remoteNote: FieldNote,
    strategy: ConflictStrategy,
    options: ManualMergeOptions = {},
  ): FieldNote {
    if (localNote.note_id !== remoteNote.note_id) {
      throw new InvalidConflictResolutionError(
        `Cannot resolve conflict across mismatched note IDs: local='${localNote.note_id}' vs remote='${remoteNote.note_id}'.`,
      );
    }
    if (!VALID_CONFLICT_STRATEGIES.includes(strategy)) {
      throw new InvalidConflictResolutionError(
        `Unsupported conflict resolution strategy: ${String(strategy)}.`,
      );
    }

    const nextVersion = Math.max(localNote.version, remoteNote.version) + 1;
    const nextLamport = Math.max(localNote.lamport_ts, remoteNote.lamport_ts) + 1;
    const resolvedUpdatedAt = options.updatedAt ?? new Date().toISOString();

    if (strategy === "KEEP_LOCAL") {
      return validateFieldNote({
        ...localNote,
        version: nextVersion,
        lamport_ts: nextLamport,
        sync_state: "PENDING_PUSH",
        updated_at: resolvedUpdatedAt,
      });
    }

    if (strategy === "ACCEPT_REMOTE") {
      return validateFieldNote({
        ...remoteNote,
        version: nextVersion,
        lamport_ts: nextLamport,
        sync_state: "PENDING_PUSH",
        updated_at: resolvedUpdatedAt,
      });
    }

    // MANUAL_MERGE
    const mergedTitle = options.mergedTitle?.trim() ?? "";
    const mergedBody = options.mergedBody?.trim() ?? "";
    if (!mergedTitle) {
      throw new InvalidConflictResolutionError(
        "MANUAL_MERGE strategy requires a non-empty 'mergedTitle'.",
      );
    }
    if (!mergedBody) {
      throw new InvalidConflictResolutionError(
        "MANUAL_MERGE strategy requires a non-empty 'mergedBody'.",
      );
    }

    return validateFieldNote({
      note_id: localNote.note_id,
      site_code: localNote.site_code,
      title: mergedTitle,
      body: mergedBody,
      severity: localNote.severity,
      location: localNote.location,
      author_device_id: localNote.author_device_id,
      version: nextVersion,
      lamport_ts: nextLamport,
      sync_state: "PENDING_PUSH",
      updated_at: resolvedUpdatedAt,
      deleted: false,
    });
  }
}
