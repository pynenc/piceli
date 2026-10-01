import { describe, expect, it } from 'vitest';
import type { Resource } from '../../api/generated';
import { orderByOwner } from './topology';

const resource = (kind: string, name: string, uid: string, owner_uids: string[] = []) => ({
  id: uid,
  identity: { kind, name, uid },
  owner_uids,
} as Resource);

describe('resource relationship view', () => {
  it('shows every table resource once with its observed owner depth', () => {
    const deployment = resource('Deployment', 'api', 'deployment');
    const replica = resource('ReplicaSet', 'api-abc', 'replica', ['deployment']);
    const pod = resource('Pod', 'api-abc-1', 'pod', ['replica']);
    const external = resource('Pod', 'other', 'other', ['unknown-owner']);
    const rows = orderByOwner([pod, external, replica, deployment]);
    expect(rows.map(item => [item.resource.id, item.depth])).toEqual([
      ['deployment', 0], ['replica', 1], ['pod', 2], ['other', 0],
    ]);
  });

  it('retains cyclic or partially observed resources without looping', () => {
    const rows = orderByOwner([
      resource('Pod', 'one', 'one', ['two']),
      resource('Pod', 'two', 'two', ['one']),
    ]);
    expect(rows.map(item => item.resource.id).sort()).toEqual(['one', 'two']);
  });
});
