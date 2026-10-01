// Independent synthetic oracle for the proposed checkpoint contract.
// This exercises an in-memory reference machine, not ephy-worker production.
import assert from 'node:assert/strict';
import { test } from 'node:test';

const identity = Object.freeze({
  job_id: 'research-synthetic', run_id: 'run-synthetic',
  profile_id: 'fixture', input_sha256: 'input-1',
  target_revision: 'target-1', policy_revision: 'policy-1',
});

function ready(overrides = {}) {
  return {
    schema_version: 1, checkpoint_revision: 3, attempt_id: 'attempt-0',
    ...identity, stage: 'search', round: 0, cursor: 0,
    budget: { model_requests: 1, search_requests: 0, model_limit: 3 },
    fence_epoch: 0, owner: null, operation: null, ...overrides,
  };
}

function copy(value) { return structuredClone(value); }
const verifiedProcessExit = Symbol('trusted supervisor observed process termination');

class FakeDurableHead {
  constructor(checkpoint) { this.head = copy(checkpoint); this.effects = []; }

  #publish(expectedRevision, next, { failWrite = false } = {}) {
    if (this.head.checkpoint_revision !== expectedRevision) throw Error('checkpoint_conflict');
    if (failWrite) throw Error('checkpoint_write_failed');
    this.head = copy({ ...next, checkpoint_revision: expectedRevision + 1 });
  }

  assertOwner(credential) {
    const owner = this.head.owner;
    if (owner === null || owner.attempt_id !== credential?.attempt_id ||
        owner.fence_token !== credential?.fence_token) {
      throw Error('checkpoint_owner_mismatch');
    }
  }

  resume(request, expectedRevision, attemptId) {
    const current = this.head;
    for (const key of Object.keys(identity)) {
      if (current[key] !== request[key]) throw Error(`checkpoint_${key}_mismatch`);
    }
    if (current.schema_version !== 1) throw Error('checkpoint_schema_unsupported');
    if (current.checkpoint_revision !== expectedRevision) throw Error('checkpoint_conflict');
    if (current.operation !== null) throw Error('checkpoint_operation_uncertain');
    if (current.owner !== null) throw Error('checkpoint_active_owner');
    const fenceToken = current.fence_epoch + 1;
    const next = copy({ ...current, attempt_id: attemptId, fence_epoch: fenceToken,
      owner: { attempt_id: attemptId, fence_token: fenceToken } });
    this.#publish(expectedRevision, next);
    return copy(this.head);
  }

  release(credential) {
    this.assertOwner(credential);
    const current = this.head;
    if (current.operation !== null) throw Error('checkpoint_operation_uncertain');
    this.#publish(current.checkpoint_revision, { ...current, owner: null });
  }

  confirmStopped(credential, proof) {
    this.assertOwner(credential);
    if (proof !== verifiedProcessExit) throw Error('checkpoint_owner_not_proven_stopped');
    if (this.head.operation !== null) throw Error('checkpoint_operation_uncertain');
    this.#publish(this.head.checkpoint_revision, { ...this.head, owner: null });
  }

  beginExternal(credential, kind, { failWrite = false } = {}) {
    this.assertOwner(credential);
    const current = this.head;
    if (current.operation !== null) throw Error('checkpoint_operation_uncertain');
    if (kind === 'model' && current.budget.model_requests >= current.budget.model_limit) {
      throw Error('model_requests');
    }
    const next = copy(current);
    if (kind === 'model') next.budget.model_requests += 1;
    if (kind === 'search') next.budget.search_requests += 1;
    next.operation = { id: `op-${current.checkpoint_revision + 1}`, kind, state: 'inflight',
      attempt_id: credential.attempt_id, fence_token: credential.fence_token };
    this.#publish(current.checkpoint_revision, next, { failWrite });
    // This is the only external-effect point in the reference machine.
    this.effects.push(next.operation.id);
  }

  completeExternal(credential, nextStage, { failWrite = false } = {}) {
    this.assertOwner(credential);
    const current = this.head;
    if (current.operation === null) throw Error('no_operation');
    if (current.operation.attempt_id !== credential.attempt_id ||
        current.operation.fence_token !== credential.fence_token) {
      throw Error('checkpoint_operation_owner_mismatch');
    }
    const next = copy({ ...current, operation: null, stage: nextStage, cursor: current.cursor + 1 });
    this.#publish(current.checkpoint_revision, next, { failWrite });
  }
}

test('restart at committed plan boundary preserves cursor and budget without replay', () => {
  const store = new FakeDurableHead(ready());
  const resumed = store.resume(identity, 3, 'attempt-1');
  assert.equal(resumed.stage, 'search');
  assert.equal(resumed.cursor, 0);
  assert.equal(resumed.budget.model_requests, 1);
  assert.deepEqual(store.effects, []);
});

test('double resume of the same revision admits only one attempt', () => {
  const store = new FakeDurableHead(ready());
  store.resume(identity, 3, 'attempt-1');
  assert.throws(() => store.resume(identity, 3, 'attempt-2'), /checkpoint_conflict/);
  assert.equal(store.head.attempt_id, 'attempt-1');
  assert.deepEqual(store.effects, []);
});

test('stale job, run, profile, input, target, or policy is rejected before effects', () => {
  for (const key of Object.keys(identity)) {
    const store = new FakeDurableHead(ready());
    assert.throws(() => store.resume({ ...identity, [key]: 'stale' }, 3, 'attempt-1'), /mismatch/);
    assert.equal(store.head.checkpoint_revision, 3);
    assert.deepEqual(store.effects, []);
  }
});

test('cumulative model budget survives restart and blocks another request', () => {
  const store = new FakeDurableHead(ready({ budget: { model_requests: 3, search_requests: 2, model_limit: 3 } }));
  const owner = store.resume(identity, 3, 'attempt-1').owner;
  assert.throws(() => store.beginExternal(owner, 'model'), /model_requests/);
  assert.equal(store.head.budget.model_requests, 3);
  assert.equal(store.head.budget.search_requests, 2);
  assert.deepEqual(store.effects, []);
});

test('failure to publish intent leaves prior ready checkpoint and performs no effect', () => {
  const store = new FakeDurableHead(ready());
  const owner = store.resume(identity, 3, 'attempt-1').owner;
  assert.throws(() => store.beginExternal(owner, 'search', { failWrite: true }), /checkpoint_write_failed/);
  assert.equal(store.head.checkpoint_revision, 4);
  assert.equal(store.head.operation, null);
  assert.deepEqual(store.effects, []);
});

test('failure after effect leaves durable intent and refuses automatic replay', () => {
  const store = new FakeDurableHead(ready());
  const owner = store.resume(identity, 3, 'attempt-1').owner;
  store.beginExternal(owner, 'search');
  assert.deepEqual(store.effects, ['op-5']);
  assert.throws(() => store.completeExternal(owner, 'select', { failWrite: true }), /checkpoint_write_failed/);
  assert.equal(store.head.operation.state, 'inflight');
  assert.throws(() => store.release(owner), /checkpoint_operation_uncertain/);
  assert.throws(() => store.confirmStopped(owner, verifiedProcessExit), /checkpoint_operation_uncertain/);
  assert.throws(() => store.resume(identity, 5, 'attempt-2'), /checkpoint_operation_uncertain/);
  assert.deepEqual(store.effects, ['op-5']);
});

test('completed effect advances exactly one cursor and preserves its charge', () => {
  const store = new FakeDurableHead(ready());
  const owner = store.resume(identity, 3, 'attempt-1').owner;
  store.beginExternal(owner, 'search');
  store.completeExternal(owner, 'search');
  assert.equal(store.head.cursor, 1);
  assert.equal(store.head.budget.search_requests, 1);
  assert.equal(store.head.operation, null);
  assert.deepEqual(store.effects, ['op-5']);
});

test('wrong owner cannot dispatch or commit an external operation', () => {
  const store = new FakeDurableHead(ready());
  const owner = store.resume(identity, 3, 'attempt-1').owner;
  const wrong = { attempt_id: 'attempt-2', fence_token: owner.fence_token };
  assert.throws(() => store.beginExternal(wrong, 'search'), /checkpoint_owner_mismatch/);
  store.beginExternal(owner, 'search');
  assert.throws(() => store.completeExternal(wrong, 'select'), /checkpoint_owner_mismatch/);
  assert.deepEqual(store.effects, ['op-5']);
});

test('an active owner blocks takeover even with the current revision or expired lease', () => {
  const store = new FakeDurableHead(ready());
  store.resume(identity, 3, 'attempt-1');
  store.head.owner.lease_expired = true; // Time passage is not process termination.
  assert.throws(() => store.resume(identity, 4, 'attempt-2'), /checkpoint_active_owner/);
  assert.equal(store.head.owner.attempt_id, 'attempt-1');
  assert.deepEqual(store.effects, []);
});

test('release fences old owner before a new attempt can perform effects', () => {
  const store = new FakeDurableHead(ready());
  const oldOwner = store.resume(identity, 3, 'attempt-1').owner;
  store.release(oldOwner);
  const newOwner = store.resume(identity, 5, 'attempt-2').owner;
  assert.notEqual(newOwner.fence_token, oldOwner.fence_token);
  store.beginExternal(newOwner, 'search');
  assert.throws(() => store.beginExternal(oldOwner, 'search'), /checkpoint_owner_mismatch/);
  assert.throws(() => store.completeExternal(oldOwner, 'select'), /checkpoint_owner_mismatch/);
  assert.deepEqual(store.effects, ['op-7']);
});

test('abandoned owner requires verified stop before a new fenced attempt', () => {
  const store = new FakeDurableHead(ready());
  const oldOwner = store.resume(identity, 3, 'attempt-1').owner;
  assert.throws(() => store.confirmStopped(oldOwner, { lease_expired: true }), /not_proven_stopped/);
  store.confirmStopped(oldOwner, verifiedProcessExit);
  const newOwner = store.resume(identity, 5, 'attempt-2').owner;
  assert.throws(() => store.beginExternal(oldOwner, 'search'), /checkpoint_owner_mismatch/);
  assert.equal(newOwner.fence_token, oldOwner.fence_token + 1);
  assert.deepEqual(store.effects, []);
});
