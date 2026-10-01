import { useEffect, useState } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { api } from '../../api/client';

type Profiles = { active: string | null; profiles: { name: string; available: boolean }[] };

export function ProfilePicker() {
  const query = useQuery({ queryKey: ['profiles'], queryFn: ({ signal }) => api.profiles(signal).then(value => value as Profiles) });
  const [selected, setSelected] = useState('');
  const switcher = useMutation({ mutationFn: (name: string) => api.switchProfile({ name }) });
  useEffect(() => { if (query.data) setSelected(query.data.active ?? ''); }, [query.data]);
  if (!query.data || !query.data.profiles.length) return <span className="hosting">Local UI</span>;
  const { active, profiles } = query.data;
  return <div className="profile-picker" aria-label="Credential profile">
    <span className="small">Profile: <strong>{active ?? 'None'}</strong></span>
    {profiles.length > 1 && <><label className="sr-only" htmlFor="piceli-profile">Choose credential profile</label><select id="piceli-profile" aria-label="Choose credential profile" value={selected} onChange={event => setSelected(event.target.value)} disabled={switcher.isPending || switcher.isSuccess}><option value="">Choose profile</option>{profiles.map(item => <option key={item.name} value={item.name} disabled={!item.available}>{item.name}{item.available ? '' : ' (unavailable)'}</option>)}</select><button disabled={!selected || selected === active || switcher.isPending || switcher.isSuccess} onClick={() => switcher.mutate(selected)}>Switch</button></>}
    {switcher.isSuccess && <span role="status" className="small">Restarting. Reopen the new local launch address shown by Piceli.</span>}
    {switcher.isError && <span role="alert" className="small">The profile could not be selected. Check it with <code>piceli profiles --json</code>.</span>}
  </div>;
}
