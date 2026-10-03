/**
 * T2.12 — catalogue administration (design §7.4; C1, C2, C3, C4).
 *
 * The criterion: "an admin can define a category, add custom fields, and create
 * an item type without leaving the screen." So all three live here as one page
 * with three panes rather than three routes — defining an item type means
 * choosing its category, and bouncing between screens to create the category
 * first is how half-finished catalogues happen.
 *
 * C1's inheritance is shown rather than implied: a category with no criticality
 * of its own displays the one it inherits, because that inherited value is what
 * actually routes an approval (§5.1) and an admin who cannot see it is guessing.
 */

import { type ReactNode, useEffect, useState } from 'react';
import { useForm } from 'react-hook-form';

import { PERM } from '../../auth/permissions';
import { useSession } from '../../auth/session';
import { applyFieldErrors, errorMessage, useAction, useList } from '../../api/hooks';
import {
  Banner,
  Button,
  Card,
  Checkbox,
  Field,
  Input,
  Select,
  Spinner,
  Textarea,
} from '../../components/ui';
import { EmptyState, PageHeader, Sheet } from '../../components/ui/data';
import type {
  CategoryCustomField,
  Criticality,
  ItemCategory,
  ItemType,
  TrackingMode,
} from './types';

const CRITICALITIES: Criticality[] = ['LOW', 'MEDIUM', 'HIGH'];
const TRACKING_MODES: { value: TrackingMode; label: string; hint: string }[] = [
  { value: 'BULK', label: 'Bulk', hint: 'Counted or measured. Clamps, ties, brackets.' },
  {
    value: 'SERIALIZED',
    label: 'Serialized',
    hint: 'Each unit tracked by serial number. Radios, antennas.',
  },
  {
    value: 'REEL',
    label: 'Reel',
    hint: 'A drum with a remaining length. Feeder, fibre.',
  },
];

const FIELD_TYPES = [
  { value: 'TEXT', label: 'Text' },
  { value: 'NUMBER', label: 'Number' },
  { value: 'DATE', label: 'Date' },
  { value: 'BOOLEAN', label: 'Yes or no' },
  { value: 'CHOICE', label: 'One of a list' },
];

export default function CataloguePage() {
  const [selectedCategory, setSelectedCategory] = useState<number | null>(null);
  const [categorySheet, setCategorySheet] = useState(false);
  const [fieldSheet, setFieldSheet] = useState(false);
  const [itemSheet, setItemSheet] = useState(false);
  // C10: the item being changed, or null when the sheet is creating one.
  const [editing, setEditing] = useState<ItemType | null>(null);
  const { has } = useSession();
  const canManage = has(PERM.CATALOGUE_MANAGE);

  const categories = useList<ItemCategory>('item-categories', { page_size: 200 });
  const items = useList<ItemType>('item-types', {
    page_size: 200,
    category: selectedCategory ?? undefined,
  });

  const categoryRows = categories.data?.results ?? [];
  const chosen = categoryRows.find((row) => row.id === selectedCategory) ?? null;

  return (
    <div className="flex flex-col gap-4">
      <PageHeader
        title="Catalogue"
        subtitle="Categories, the fields each one asks for, and the item types inside them."
        actions={
          <>
            <Button variant="secondary" onClick={() => setCategorySheet(true)}>
              New category
            </Button>
            {canManage ? (
              <Button
                onClick={() => {
                  setEditing(null);
                  setItemSheet(true);
                }}
              >
                New item type
              </Button>
            ) : null}
          </>
        }
      />

      {categories.isError ? (
        <Banner tone="error">{errorMessage(categories.error)}</Banner>
      ) : null}

      <div className="grid gap-4 lg:grid-cols-[minmax(0,20rem)_minmax(0,1fr)]">
        <Card className="flex flex-col gap-2">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-semibold text-slate-900">Categories</h2>
            {categories.isLoading ? <Spinner className="text-slate-400" /> : null}
          </div>

          {categoryRows.length === 0 && !categories.isLoading ? (
            <EmptyState
              title="No categories yet."
              hint="A new tenant is seeded with a starter set. Add your own, or rename these."
            />
          ) : (
            <ul className="flex flex-col">
              <li>
                <button
                  type="button"
                  onClick={() => setSelectedCategory(null)}
                  className={rowClass(selectedCategory === null)}
                >
                  <span>All item types</span>
                </button>
              </li>
              {categoryRows.map((category) => (
                <li key={category.id}>
                  <button
                    type="button"
                    onClick={() => setSelectedCategory(category.id)}
                    className={rowClass(selectedCategory === category.id)}
                  >
                    <span className="flex flex-col text-left">
                      <span className={category.is_archived ? 'line-through' : undefined}>
                        {category.parent ? '— ' : ''}
                        {category.name}
                      </span>
                      <span className="text-xs text-slate-500">
                        {category.criticality
                          ? `${category.criticality.toLowerCase()} criticality`
                          : `inherits ${category.effective_criticality.toLowerCase()}`}
                      </span>
                    </span>
                    <span className="text-xs text-slate-400">{category.item_type_count ?? ''}</span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </Card>

        <div className="flex flex-col gap-4">
          {chosen ? (
            <Card className="flex flex-col gap-3">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <h2 className="text-sm font-semibold text-slate-900">
                  Fields asked for on {chosen.name}
                </h2>
                <Button variant="secondary" onClick={() => setFieldSheet(true)}>
                  Add a field
                </Button>
              </div>
              <p className="text-sm text-slate-600">
                A category can ask for its own details — a radio needs a
                frequency band, a cable does not. Required fields are enforced at
                gate-in, which is when the answer is actually to hand.
              </p>
              <CustomFieldList category={chosen} />
            </Card>
          ) : null}

          <Card className="flex flex-col gap-3">
            <h2 className="text-sm font-semibold text-slate-900">
              {chosen ? `Item types in ${chosen.name}` : 'All item types'}
            </h2>
            {items.isLoading ? (
              <Spinner className="text-slate-400" />
            ) : (
              <ItemRows
                rows={items.data?.results ?? []}
                empty={
                  <EmptyState
                    title="No item types here."
                    hint="An item type is what a gate-in line refers to."
                    action={
                      canManage ? (
                        <Button
                          onClick={() => {
                            setEditing(null);
                            setItemSheet(true);
                          }}
                        >
                          New item type
                        </Button>
                      ) : undefined
                    }
                  />
                }
                onOpen={(row) => {
                  setEditing(row);
                  setItemSheet(true);
                }}
              />
            )}
          </Card>
        </div>
      </div>

      <CategorySheet
        open={categorySheet}
        onClose={() => setCategorySheet(false)}
        categories={categoryRows}
      />
      {chosen ? (
        <CustomFieldSheet
          open={fieldSheet}
          onClose={() => setFieldSheet(false)}
          category={chosen}
        />
      ) : null}
      <ItemTypeSheet
        open={itemSheet}
        item={editing ?? undefined}
        onClose={() => setItemSheet(false)}
        categories={categoryRows}
        defaultCategory={selectedCategory}
      />
    </div>
  );
}

/**
 * C10: every row is a button that opens the item. One list at every width: a
 * table row with a click handler cannot be reached from the keyboard, and the
 * accessible name has to say which item it opens.
 */
function ItemRows({
  rows,
  empty,
  onOpen,
}: {
  rows: ItemType[];
  empty: ReactNode;
  onOpen: (row: ItemType) => void;
}) {
  if (rows.length === 0) return <>{empty}</>;
  return (
    <ul className="flex flex-col gap-2">
      {rows.map((row) => (
        <li key={row.id}>
          <button
            type="button"
            aria-label={`Edit ${row.name}`}
            onClick={() => onOpen(row)}
            className="flex min-h-[44px] w-full flex-col gap-1 rounded-xl border border-slate-200 bg-white p-3 text-left hover:bg-slate-50 active:bg-slate-50"
          >
            <span
              className={[
                'text-sm font-medium',
                row.is_archived ? 'text-slate-400 line-through' : 'text-slate-900',
              ].join(' ')}
            >
              {row.name}
            </span>
            <span className="text-xs text-slate-500">
              {[
                row.is_archived ? 'archived' : null,
                row.code,
                row.default_tracking_mode.toLowerCase(),
                row.uom,
                `${row.criticality.toLowerCase()} criticality`,
                row.is_returnable ? `back in ${row.default_return_days ?? '?'} days` : null,
              ]
                .filter(Boolean)
                .join(' · ')}
            </span>
          </button>
        </li>
      ))}
    </ul>
  );
}

function rowClass(active: boolean) {
  return [
    'flex min-h-[44px] w-full items-center justify-between gap-2 rounded-lg px-2 text-sm',
    active ? 'bg-slate-900 text-white' : 'text-slate-800 hover:bg-slate-100',
  ].join(' ');
}

function CustomFieldList({ category }: { category: ItemCategory }) {
  const fields = useList<CategoryCustomField>('category-custom-fields', {
    category: category.id,
    page_size: 100,
  });

  const rows = fields.data?.results ?? [];
  if (fields.isLoading) return <Spinner className="text-slate-400" />;
  if (rows.length === 0) {
    return <p className="text-sm text-slate-500">No extra fields on this category.</p>;
  }

  return (
    <ul className="flex flex-col gap-1 text-sm">
      {rows.map((field) => (
        <li key={field.id} className="flex flex-wrap items-baseline gap-2">
          <span className="font-medium text-slate-900">{field.label}</span>
          <span className="text-slate-500">{field.field_type.toLowerCase()}</span>
          {field.required_at_gate_in ? (
            <span className="rounded-full bg-amber-100 px-2 py-0.5 text-xs text-amber-900">
              required at gate-in
            </span>
          ) : null}
          {field.options.length ? (
            <span className="text-xs text-slate-500">{field.options.join(', ')}</span>
          ) : null}
        </li>
      ))}
    </ul>
  );
}

interface CategoryForm {
  name: string;
  code: string;
  parent: string;
  criticality: string;
}

function CategorySheet({
  open,
  onClose,
  categories,
}: {
  open: boolean;
  onClose: () => void;
  categories: ItemCategory[];
}) {
  const form = useForm<CategoryForm>({
    defaultValues: { name: '', code: '', parent: '', criticality: '' },
  });
  const [banner, setBanner] = useState<string | null>(null);
  const create = useAction<Record<string, unknown>>({ resource: 'item-categories' });

  const submit = form.handleSubmit(async (values) => {
    setBanner(null);
    try {
      await create.mutateAsync({
        name: values.name,
        code: values.code || undefined,
        parent: values.parent ? Number(values.parent) : null,
        // C1: left empty, it inherits from its parent — which is the point of
        // the tree, so an empty value is a real answer here.
        criticality: values.criticality || null,
      });
      form.reset();
      onClose();
    } catch (error) {
      setBanner(applyFieldErrors(error, form.setError));
    }
  });

  return (
    <Sheet
      open={open}
      title="New category"
      onClose={onClose}
      footer={
        <>
          <Button variant="secondary" onClick={onClose} block>
            Cancel
          </Button>
          <Button onClick={submit} loading={create.isPending} block>
            Create
          </Button>
        </>
      }
    >
      <form className="flex flex-col gap-3" onSubmit={submit}>
        {banner ? <Banner tone="error">{banner}</Banner> : null}

        <Field label="Name" htmlFor="category-name" error={form.formState.errors.name?.message}>
          <Input id="category-name" {...form.register('name', { required: 'A name is required.' })} />
        </Field>

        <Field label="Code" htmlFor="category-code" hint="Optional short code.">
          <Input id="category-code" {...form.register('code')} />
        </Field>

        <Field
          label="Inside"
          htmlFor="category-parent"
          hint="Leave empty for a top-level category."
        >
          <Select id="category-parent" {...form.register('parent')}>
            <option value="">Top level</option>
            {categories.map((category) => (
              <option key={category.id} value={category.id}>
                {category.name}
              </option>
            ))}
          </Select>
        </Field>

        <Field
          label="Criticality"
          htmlFor="category-criticality"
          hint="Drives which approvals a gate-out needs. Empty inherits from the parent."
        >
          <Select id="category-criticality" {...form.register('criticality')}>
            <option value="">Inherit</option>
            {CRITICALITIES.map((level) => (
              <option key={level} value={level}>
                {level.toLowerCase()}
              </option>
            ))}
          </Select>
        </Field>
      </form>
    </Sheet>
  );
}

interface FieldForm {
  label: string;
  key: string;
  field_type: string;
  required_at_gate_in: boolean;
  options: string;
}

function CustomFieldSheet({
  open,
  onClose,
  category,
}: {
  open: boolean;
  onClose: () => void;
  category: ItemCategory;
}) {
  const form = useForm<FieldForm>({
    defaultValues: {
      label: '',
      key: '',
      field_type: 'TEXT',
      required_at_gate_in: false,
      options: '',
    },
  });
  const [banner, setBanner] = useState<string | null>(null);
  const create = useAction<Record<string, unknown>>({
    resource: 'category-custom-fields',
    invalidates: ['category-custom-fields', 'item-categories'],
  });
  const fieldType = form.watch('field_type');

  const submit = form.handleSubmit(async (values) => {
    setBanner(null);
    try {
      await create.mutateAsync({
        category: category.id,
        label: values.label,
        key: values.key || undefined,
        field_type: values.field_type,
        required_at_gate_in: values.required_at_gate_in,
        options: values.options
          ? values.options.split(',').map((option) => option.trim()).filter(Boolean)
          : [],
      });
      form.reset();
      onClose();
    } catch (error) {
      setBanner(applyFieldErrors(error, form.setError));
    }
  });

  return (
    <Sheet
      open={open}
      title={`Field on ${category.name}`}
      onClose={onClose}
      footer={
        <>
          <Button variant="secondary" onClick={onClose} block>
            Cancel
          </Button>
          <Button onClick={submit} loading={create.isPending} block>
            Add field
          </Button>
        </>
      }
    >
      <form className="flex flex-col gap-3" onSubmit={submit}>
        {banner ? <Banner tone="error">{banner}</Banner> : null}

        <Field label="Label" htmlFor="field-label" error={form.formState.errors.label?.message}>
          <Input
            id="field-label"
            placeholder="Frequency band"
            {...form.register('label', { required: 'A label is required.' })}
          />
        </Field>

        <Field label="Type" htmlFor="field-type">
          <Select id="field-type" {...form.register('field_type')}>
            {FIELD_TYPES.map((type) => (
              <option key={type.value} value={type.value}>
                {type.label}
              </option>
            ))}
          </Select>
        </Field>

        {fieldType === 'CHOICE' ? (
          <Field label="Choices" htmlFor="field-options" hint="Separated by commas.">
            <Textarea id="field-options" placeholder="700, 800, 1800, 2600" {...form.register('options')} />
          </Field>
        ) : null}

        <Checkbox
          id="field-required"
          label="Required at gate-in"
          hint="Enforced when the delivery is received, which is when the answer is to hand."
          {...form.register('required_at_gate_in')}
        />
      </form>
    </Sheet>
  );
}

interface ItemForm {
  category: string;
  name: string;
  code: string;
  uom: string;
  default_tracking_mode: TrackingMode;
  is_returnable: boolean;
  default_return_days: string;
  min_stock_qty: string;
  description: string;
}

export function ItemTypeSheet({
  open,
  onClose,
  categories: givenCategories,
  defaultCategory = null,
  onCreated,
  item,
}: {
  open: boolean;
  onClose: () => void;
  /** The catalogue screen has these; a form elsewhere does not, so fetch them. */
  categories?: ItemCategory[];
  defaultCategory?: number | null;
  onCreated?: (record: { id: number }) => void;
  /** C10: present means edit mode: the sheet opens filled in and saves in place. */
  item?: ItemType;
}) {
  const fetchedCategories = useList<ItemCategory>('item-categories', { page_size: 200 }, {
    enabled: givenCategories === undefined,
  });
  const categories = givenCategories ?? fetchedCategories.data?.results ?? [];
  const { has } = useSession();
  const canManage = has(PERM.CATALOGUE_MANAGE);
  const emptyForm: ItemForm = {
    category: defaultCategory ? String(defaultCategory) : '',
    name: '',
    code: '',
    uom: 'ea',
    default_tracking_mode: 'BULK',
    is_returnable: false,
    default_return_days: '',
    min_stock_qty: '',
    description: '',
  };
  const form = useForm<ItemForm>({
    defaultValues: item ? valuesOf(item) : emptyForm,
  });
  const locked = Boolean(item?.tracking_locked);
  const [confirmingArchive, setConfirmingArchive] = useState(false);
  const [banner, setBanner] = useState<string | null>(null);

  // The sheet stays mounted and is reused, so it is refilled whenever it opens
  // (on a different item, or on none) rather than relying on first defaults.
  useEffect(() => {
    if (!open) return;
    form.reset(item ? valuesOf(item) : emptyForm);
    setBanner(null);
    setConfirmingArchive(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, item?.id]);

  const create = useAction<Record<string, unknown>, { id: number }>({ resource: 'item-types' });
  const save = useAction<Record<string, unknown>, { id: number }>({
    resource: 'item-types',
    method: 'patch',
    path: () => String(item?.id),
  });
  const archive = useAction<{ archive: boolean }>({
    resource: 'item-types',
    path: (body) => `${item?.id}/${body.archive ? 'archive' : 'unarchive'}`,
  });

  const trackingMode = form.watch('default_tracking_mode');
  const returnable = form.watch('is_returnable');

  const submit = form.handleSubmit(async (values) => {
    setBanner(null);
    try {
      const body: Record<string, unknown> = {
        category: Number(values.category),
        name: values.name,
        // Editing sends the blank too, so a code can be cleared (C10).
        code: item ? values.code.trim() : values.code || undefined,
        description: values.description,
        uom: values.uom,
        default_tracking_mode: values.default_tracking_mode,
        is_returnable: values.is_returnable,
        default_return_days: values.default_return_days
          ? Number(values.default_return_days)
          : null,
        min_stock_qty: values.min_stock_qty || null,
      };
      if (item) {
        // C10: a locked item's tracking mode and unit are not sent at all, so
        // saving a rename never trips the server's refusal.
        if (locked) {
          delete body.uom;
          delete body.default_tracking_mode;
        }
        await save.mutateAsync(body);
        onClose();
        return;
      }
      const created = await create.mutateAsync(body);
      form.reset();
      onClose();
      onCreated?.(created);
    } catch (error) {
      setBanner(applyFieldErrors(error, form.setError));
    }
  });

  const toggleArchive = async () => {
    setBanner(null);
    try {
      await archive.mutateAsync({ archive: !item?.is_archived });
      setConfirmingArchive(false);
      onClose();
    } catch (error) {
      setBanner(errorMessage(error));
    }
  };

  const selectedMode = TRACKING_MODES.find((mode) => mode.value === trackingMode);

  return (
    <Sheet
      open={open}
      title={item ? (canManage ? `Change ${item.name}` : item.name) : 'New item type'}
      onClose={onClose}
      footer={
        item && !canManage ? (
          <Button variant="secondary" onClick={onClose} block>
            Close
          </Button>
        ) : (
          <>
            <Button variant="secondary" onClick={onClose} block>
              Cancel
            </Button>
            <Button onClick={submit} loading={create.isPending || save.isPending} block>
              {item ? 'Save changes' : 'Create'}
            </Button>
          </>
        )
      }
    >
      <form className="flex flex-col gap-3" onSubmit={submit}>
        {banner ? <Banner tone="error">{banner}</Banner> : null}
        {/* C10: without catalogue.manage the sheet only answers "what is this set up as". */}
        <fieldset disabled={Boolean(item) && !canManage} className="flex flex-col gap-3">

        <Field
          label="Category"
          htmlFor="item-category"
          error={form.formState.errors.category?.message}
        >
          <Select
            id="item-category"
            invalid={Boolean(form.formState.errors.category)}
            {...form.register('category', { required: 'Choose a category.' })}
          >
            <option value="">Choose…</option>
            {categories.map((category) => (
              <option key={category.id} value={category.id}>
                {category.name}
              </option>
            ))}
          </Select>
        </Field>

        <Field label="Name" htmlFor="item-name" error={form.formState.errors.name?.message}>
          <Input
            id="item-name"
            placeholder="RRU 2600 40W"
            {...form.register('name', { required: 'A name is required.' })}
          />
        </Field>

        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Code" htmlFor="item-code" error={form.formState.errors.code?.message}>
            <Input id="item-code" {...form.register('code')} />
          </Field>
          <Field
            label="Unit"
            htmlFor="item-uom"
            hint={trackingMode === 'REEL' ? 'A length, such as m.' : 'ea, m, box.'}
            error={form.formState.errors.uom?.message}
          >
            <Input id="item-uom" disabled={locked} {...form.register('uom', { required: true })} />
          </Field>
        </div>

        <Field
          label="How it is tracked"
          htmlFor="item-tracking"
          hint={selectedMode?.hint}
          error={form.formState.errors.default_tracking_mode?.message}
        >
          <Select
            id="item-tracking"
            disabled={locked}
            {...form.register('default_tracking_mode', {
              // A reel is measured, not counted (D12), and the server refuses a
              // reel in "ea". Switch the obvious default rather than let the
              // storekeeper meet the refusal.
              onChange: (event) => {
                if (event.target.value === 'REEL' && form.getValues('uom').trim() === 'ea') {
                  form.setValue('uom', 'm', { shouldDirty: true });
                }
              },
            })}
          >
            {TRACKING_MODES.map((mode) => (
              <option key={mode.value} value={mode.value}>
                {mode.label}
              </option>
            ))}
          </Select>
        </Field>

        <Checkbox
          id="item-returnable"
          label="Comes back"
          hint="A tool or a test set is expected back, and goes on somebody's record until it is."
          {...form.register('is_returnable')}
        />

        {returnable ? (
          <Field
            label="Expected back within"
            htmlFor="item-return-days"
            hint="Days. Sets the return date when it is issued."
          >
            <Input
              id="item-return-days"
              type="number"
              inputMode="numeric"
              min={1}
              {...form.register('default_return_days')}
            />
          </Field>
        ) : null}

        <Field
          label="Reorder level"
          htmlFor="item-min"
          hint="Below this, it appears on the low-stock list. Optional."
        >
          <Input id="item-min" inputMode="decimal" {...form.register('min_stock_qty')} />
        </Field>

        <Field label="Description" htmlFor="item-description">
          <Textarea id="item-description" {...form.register('description')} />
        </Field>
        </fieldset>

        {locked ? (
          <p className="text-sm text-slate-600">
            Fixed: this item has stock history, so changing it would change what past movements
            mean.
          </p>
        ) : null}

        {item && canManage ? (
          confirmingArchive ? (
            <div className="flex flex-col gap-2 rounded-lg border border-amber-300 bg-amber-50 p-3">
              <p className="text-sm text-amber-900">
                {item.is_archived
                  ? `Restore ${item.name}? It will appear in pickers again.`
                  : `Archive ${item.name}? It disappears from pickers; its history stays.`}
              </p>
              <div className="flex gap-2">
                <Button variant="secondary" onClick={() => setConfirmingArchive(false)} block>
                  Keep as it is
                </Button>
                <Button onClick={toggleArchive} loading={archive.isPending} block>
                  {item.is_archived ? 'Yes, restore it' : 'Yes, archive it'}
                </Button>
              </div>
            </div>
          ) : (
            <Button variant="secondary" onClick={() => setConfirmingArchive(true)} block>
              {item.is_archived ? 'Restore this item' : 'Archive this item'}
            </Button>
          )
        ) : null}
      </form>
    </Sheet>
  );
}

function valuesOf(item: ItemType): ItemForm {
  return {
    category: String(item.category),
    name: item.name,
    code: item.code ?? '',
    uom: item.uom,
    default_tracking_mode: item.default_tracking_mode,
    is_returnable: item.is_returnable,
    default_return_days: item.default_return_days ? String(item.default_return_days) : '',
    min_stock_qty: item.min_stock_qty ?? '',
    description: item.description ?? '',
  };
}
