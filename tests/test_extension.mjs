import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {join} from 'node:path';
import {test} from 'node:test';
import {runInNewContext} from 'node:vm';

const source = readFileSync(join(import.meta.dirname, '..', 'extension', 'service_worker.js'), 'utf8');

function harness() {
  const nodes = new Map([
    ['0', {id: '0', title: '', children: ['1', '2', '4']}],
    ['1', {id: '1', parentId: '0', title: 'Bar', children: ['10']}],
    ['2', {id: '2', parentId: '0', title: 'Other', children: ['3']}],
    ['3', {id: '3', parentId: '2', title: 'Projects', children: []}],
    ['4', {id: '4', parentId: '0', title: 'Trash', children: []}],
    ['10', {id: '10', parentId: '1', title: 'Example', url: 'https://example.com'}]
  ]);
  let listener;
  let response;
  const chrome = {
    runtime: {
      connectNative: () => ({
        onMessage: {addListener: callback => { listener = callback; }},
        onDisconnect: {addListener: () => {}},
        postMessage: value => { response = value; }
      }),
      onStartup: {addListener: () => {}},
      onInstalled: {addListener: () => {}}
    },
    storage: {local: {get: async () => ({pairingCode: 'b'.repeat(32)})}},
    bookmarks: {
      get: async id => nodes.has(id) ? [{...nodes.get(id)}] : [],
      getChildren: async id => nodes.get(id).children.map(child => ({...nodes.get(child)})),
      move: async (id, options) => {
        const node = nodes.get(id);
        nodes.get(node.parentId).children = nodes.get(node.parentId).children.filter(child => child !== id);
        node.parentId = options.parentId;
        nodes.get(node.parentId).children.push(id);
        return {...node};
      },
      update: async (id, changes) => Object.assign(nodes.get(id), changes),
      create: async details => {
        const id = '20';
        const node = {id, ...details, children: []};
        nodes.set(id, node);
        nodes.get(details.parentId).children.push(id);
        return {...node};
      }
    }
  };
  runInNewContext(source, {chrome, console});
  return {
    nodes,
    async send(message) {
      response = undefined;
      await listener(message);
      return response;
    }
  };
}

test('pairs, previews, moves, and rejects a stale snapshot', async () => {
  const app = harness();
  assert.equal((await app.send({op: 'ping'})).pairing_code, 'b'.repeat(32));
  const operation = {kind: 'move', id: '10', to: '3', trash: '4'};
  const preview = await app.send({op: 'inspect', operation});
  assert.equal(preview.snapshot.item.parentId, '1');
  app.nodes.get('10').title = 'Changed';
  const stale = await app.send({op: 'execute', operation, expected: preview.snapshot});
  assert.equal(stale.ok, false);
  assert.equal(app.nodes.get('10').parentId, '1');
  app.nodes.get('10').title = 'Example';
  const applied = await app.send({op: 'execute', operation, expected: preview.snapshot});
  assert.equal(applied.ok, true);
  assert.equal(app.nodes.get('10').parentId, '3');
});

test('protects special folders and prevents duplicate folder creation', async () => {
  const app = harness();
  const missing = await app.send({op: 'inspect', operation: {kind: 'move', id: '999', to: '3', trash: '4'}});
  assert.equal(missing.ok, false);
  assert.equal(app.nodes.get('10').parentId, '1');
  const root = await app.send({op: 'inspect', operation: {kind: 'folder-rename', id: '1', title: 'Renamed', trash: '4'}});
  assert.equal(root.ok, false);
  const trashMove = await app.send({op: 'inspect', operation: {kind: 'move', id: '10', to: '4', trash: '4'}});
  assert.equal(trashMove.ok, false);
  const operation = {kind: 'folder-create', parent: '2', title: 'Projects', trash: '4'};
  const duplicate = await app.send({op: 'inspect', operation});
  assert.equal(duplicate.ok, false);
  const create = {kind: 'folder-create', parent: '2', title: 'New', trash: '4'};
  const preview = await app.send({op: 'inspect', operation: create});
  assert.equal((await app.send({op: 'execute', operation: create, expected: preview.snapshot})).ok, true);
  assert.equal((await app.send({op: 'execute', operation: create, expected: preview.snapshot})).ok, false);
  const duplicateRename = await app.send({op: 'inspect', operation: {
    kind: 'folder-rename', id: '20', title: 'Projects', trash: '4'
  }});
  assert.equal(duplicateRename.ok, false);
  const missingGuard = await app.send({op: 'inspect', operation: {kind: 'move', id: '10', to: '4'}});
  assert.equal(missingGuard.ok, false);
});

test('edits one bookmark and renames one ordinary folder', async () => {
  const app = harness();
  const edit = {kind: 'edit', id: '10', title: 'Edited', url: 'https://example.org', trash: '4'};
  const editPreview = await app.send({op: 'inspect', operation: edit});
  assert.equal((await app.send({op: 'execute', operation: edit, expected: editPreview.snapshot})).ok, true);
  assert.equal(app.nodes.get('10').title, 'Edited');
  assert.equal(app.nodes.get('10').url, 'https://example.org');
  const rename = {kind: 'folder-rename', id: '3', title: 'Renamed', trash: '4'};
  const renamePreview = await app.send({op: 'inspect', operation: rename});
  assert.equal((await app.send({op: 'execute', operation: rename, expected: renamePreview.snapshot})).ok, true);
  assert.equal(app.nodes.get('3').title, 'Renamed');
});
