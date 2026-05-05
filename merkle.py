#!/usr/bin/env python3
"""Merkle tree for tamper-proof trade history."""

import hashlib
import json


def hash_leaf(data):
    """Hash a single trade/state entry."""
    raw = json.dumps(data, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def hash_node(left, right):
    """Hash an internal node (deterministic ordering)."""
    if left > right:
        left, right = right, left
    return hashlib.sha256(f"{left}{right}".encode()).hexdigest()


class MerkleTree:
    def __init__(self, items):
        """Build a Merkle tree from a list of dicts (trades, state, etc.)."""
        self.leaves = [hash_leaf(item) for item in items]
        self.tree = [self.leaves[:]]
        self._build()

    def _build(self):
        level = self.leaves[:]
        while len(level) > 1:
            next_level = []
            for i in range(0, len(level), 2):
                left = level[i]
                right = level[i + 1] if i + 1 < len(level) else left
                next_level.append(hash_node(left, right))
            self.tree.append(next_level)
            level = next_level

    @property
    def root(self):
        if not self.tree or not self.tree[-1]:
            return hash_leaf({})
        return self.tree[-1][0]

    def proof(self, index):
        """Generate a Merkle proof for the item at `index`."""
        if index >= len(self.leaves):
            return None
        proof_path = []
        idx = index
        for level in self.tree[:-1]:
            sibling = idx ^ 1
            if sibling < len(level):
                proof_path.append(level[sibling])
            idx //= 2
        return proof_path

    @staticmethod
    def verify(item, proof_path, root):
        """Verify an item against a Merkle proof and root."""
        current = hash_leaf(item)
        for sibling in proof_path:
            current = hash_node(current, sibling)
        return current == root


def build_tick_root(state, positions, trades):
    """Build a Merkle root for a complete tick snapshot.
    
    Tree structure:
    - leaf 0: state
    - leaf 1: hash of positions list
    - leaf 2..N: individual trades
    """
    items = [
        state,
        {"positions": positions},
    ] + list(trades)
    
    tree = MerkleTree(items)
    return tree.root


def build_chain_root(roots):
    """Chain multiple tick roots into a single root (like a blockchain).
    Each root depends on the previous one, making the entire history tamper-proof."""
    if not roots:
        return hash_leaf({})
    
    items = [{"root": r, "prev": roots[i - 1] if i > 0 else ""} for i, r in enumerate(roots)]
    tree = MerkleTree(items)
    return tree.root
