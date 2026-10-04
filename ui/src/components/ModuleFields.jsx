import React from 'react';

export function ModuleFields({ fields, values, onChange, disabled }) {
  return fields.map(field => {
    const value = values[field.name], scale = field.scale || 1;
    if (field.type === 'boolean') return <label className="check-field" key={field.name}><input type="checkbox" disabled={disabled} checked={!!value} onChange={e => onChange(field.name, e.target.checked)}/>{field.label}</label>;
    const numeric = ['integer', 'number'].includes(field.type);
    return <label key={field.name}><span>{field.label || field.name}</span>{field.type === 'json' ? <textarea disabled={disabled} value={typeof value === 'string' ? value : JSON.stringify(value ?? null)} onChange={e => onChange(field.name, e.target.value)}/> : <input type={field.secret ? 'password' : numeric ? 'number' : 'text'} autoComplete={field.secret ? 'off' : undefined} required={!!field.required} min={field.min} max={field.max} maxLength={field.max_length} step={field.step || (field.type === 'integer' ? 1 : 'any')} placeholder={field.placeholder} value={value == null ? '' : numeric ? Number((value * scale).toFixed(6)) : value} disabled={disabled} onChange={e => onChange(field.name, numeric ? (e.target.value === '' ? null : Number(e.target.value) / scale) : e.target.value)}/>}</label>;
  });
}
