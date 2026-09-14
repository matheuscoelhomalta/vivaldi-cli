const HOST = "com.vivaldi_cli.bookmarks";
let port = null;

function compact(node) {
  return {
    id: node.id,
    parentId: node.parentId ?? null,
    title: node.title ?? "",
    url: node.url ?? null,
    unmodifiable: node.unmodifiable ?? null
  };
}

async function nodeById(id) {
  const nodes = await chrome.bookmarks.get(String(id));
  if (nodes.length !== 1) throw new Error(`Bookmark ${id} was not found`);
  return nodes[0];
}

function folder(node) {
  if (node.url !== undefined) throw new Error("Destination must be a folder");
  if (node.unmodifiable) throw new Error("This folder cannot be changed");
}

function bookmark(node) {
  if (node.url === undefined) throw new Error("Only individual bookmarks may be changed");
  if (node.unmodifiable) throw new Error("This bookmark cannot be changed");
}

async function outsideTrash(node, trashId) {
  let current = node;
  while (current) {
    if (current.id === trashId) throw new Error("Bookmark trash cannot be changed by this command");
    current = current.parentId ? await nodeById(current.parentId) : null;
  }
}

async function inspect(operation) {
  if (!operation?.trash) throw new Error("Bookmark trash guard is required");
  switch (operation.kind) {
    case "move": {
      const item = await nodeById(operation.id);
      const destination = await nodeById(operation.to);
      bookmark(item);
      folder(destination);
      await outsideTrash(item, operation.trash);
      await outsideTrash(destination, operation.trash);
      if (item.parentId === destination.id) throw new Error("Bookmark is already in this folder");
      return {item: compact(item), destination: compact(destination)};
    }
    case "edit": {
      const item = await nodeById(operation.id);
      bookmark(item);
      await outsideTrash(item, operation.trash);
      if (operation.title === undefined && operation.url === undefined) {
        throw new Error("Provide --title or --url");
      }
      if (operation.title === "" || operation.url === "") throw new Error("Title and URL must not be empty");
      return {item: compact(item)};
    }
    case "folder-create": {
      const parent = await nodeById(operation.parent);
      folder(parent);
      await outsideTrash(parent, operation.trash);
      if (!operation.title?.trim()) throw new Error("Folder title must not be empty");
      const children = await chrome.bookmarks.getChildren(parent.id);
      if (children.some(child => child.url === undefined && child.title === operation.title)) {
        throw new Error("A folder with that title already exists in the destination");
      }
      return {parent: compact(parent)};
    }
    case "folder-rename": {
      const item = await nodeById(operation.id);
      folder(item);
      await outsideTrash(item, operation.trash);
      if (item.parentId === "0" || item.parentId === null || item.parentId === undefined) {
        throw new Error("Special root folders cannot be renamed");
      }
      if (!operation.title?.trim()) throw new Error("Folder title must not be empty");
      const siblings = await chrome.bookmarks.getChildren(item.parentId);
      if (siblings.some(child => child.id !== item.id && child.url === undefined &&
          child.title === operation.title)) {
        throw new Error("A folder with that title already exists in the destination");
      }
      return {item: compact(item)};
    }
    default:
      throw new Error("Unsupported bookmark operation");
  }
}

async function execute(operation, expected) {
  const current = await inspect(operation);
  if (JSON.stringify(current) !== JSON.stringify(expected)) {
    throw new Error("Bookmark state changed since preview; preview again");
  }
  let result;
  switch (operation.kind) {
    case "move":
      result = await chrome.bookmarks.move(String(operation.id), {parentId: String(operation.to)});
      if (result.parentId !== String(operation.to)) throw new Error("Move could not be verified");
      break;
    case "edit": {
      const changes = {};
      if (operation.title !== undefined) changes.title = operation.title;
      if (operation.url !== undefined) changes.url = operation.url;
      result = await chrome.bookmarks.update(String(operation.id), changes);
      if (Object.entries(changes).some(([key, value]) => result[key] !== value)) {
        throw new Error("Bookmark edit could not be verified");
      }
      break;
    }
    case "folder-create":
      result = await chrome.bookmarks.create({parentId: String(operation.parent), title: operation.title});
      if (result.parentId !== String(operation.parent) || result.title !== operation.title) {
        throw new Error("Folder creation could not be verified");
      }
      break;
    case "folder-rename":
      result = await chrome.bookmarks.update(String(operation.id), {title: operation.title});
      if (result.title !== operation.title) throw new Error("Folder rename could not be verified");
      break;
  }
  return compact(await nodeById(result.id));
}

async function handle(message) {
  if (message.op === "ping") {
    const stored = await chrome.storage.local.get("pairingCode");
    return {ok: true, pairing_code: stored.pairingCode || null};
  }
  if (message.op === "inspect") return {ok: true, snapshot: await inspect(message.operation)};
  if (message.op === "execute") {
    return {ok: true, after: await execute(message.operation, message.expected)};
  }
  throw new Error("Unknown bridge request");
}

function connect() {
  if (port) return;
  try {
    port = chrome.runtime.connectNative(HOST);
  } catch (error) {
    console.error("Vivaldi CLI bridge is not available", error);
    return;
  }
  port.onMessage.addListener(async message => {
    try {
      port.postMessage(await handle(message));
    } catch (error) {
      port.postMessage({ok: false, error: String(error.message || error)});
    }
  });
  port.onDisconnect.addListener(() => {
    port = null;
    console.error("Vivaldi CLI bridge disconnected:",
                  chrome.runtime.lastError?.message || "Native Messaging port closed");
  });
}

chrome.runtime.onStartup.addListener(connect);
chrome.runtime.onInstalled.addListener(connect);
connect();
