"use client";

import {
  type Column,
  type ColumnDef,
  flexRender,
  getCoreRowModel,
  getSortedRowModel,
  type SortingState,
  useReactTable,
} from "@tanstack/react-table";
import { ArrowUpDown } from "lucide-react";
import { useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import type { TrainingSession } from "@/lib/coach-client";
import { fmtDuration, fmtInt, fmtKm, fmtShortDate } from "@/lib/format";
import { cn } from "@/lib/utils";

declare module "@tanstack/react-table" {
  interface ColumnMeta<TData, TValue> {
    align?: "left" | "right";
  }
}

function sortHeader(label: string) {
  return ({ column }: { column: Column<TrainingSession, unknown> }) => (
    <Button
      variant="ghost"
      size="sm"
      className="-ml-3 h-8 data-[align=right]:-mr-3 data-[align=right]:ml-0"
      data-align={column.columnDef.meta?.align ?? "left"}
      onClick={() => column.toggleSorting(column.getIsSorted() === "asc")}
    >
      {label}
      <ArrowUpDown className="ml-1 size-3.5 opacity-60" />
    </Button>
  );
}

const columns: ColumnDef<TrainingSession>[] = [
  {
    accessorKey: "local_start",
    header: sortHeader("Date"),
    cell: ({ row }) => (
      <span className="whitespace-nowrap">
        {fmtShortDate(row.original.local_start ?? row.original.local_date)}
      </span>
    ),
  },
  {
    accessorKey: "title",
    header: "Activity",
    cell: ({ row }) => (
      <span className="block max-w-[280px] truncate font-medium">
        {row.original.title ?? "Untitled session"}
      </span>
    ),
  },
  {
    accessorKey: "sport",
    header: "Sport",
    cell: ({ row }) =>
      row.original.sport ? (
        <Badge variant="outline" className="capitalize">
          {row.original.sport}
        </Badge>
      ) : (
        "—"
      ),
  },
  {
    id: "distance",
    accessorFn: (row) => row.distance?.value,
    header: sortHeader("Distance"),
    meta: { align: "right" },
    cell: ({ row }) =>
      fmtKm(row.original.distance?.unit === "metres" ? row.original.distance.value : undefined),
  },
  {
    id: "duration",
    accessorFn: (row) => row.duration?.value,
    header: sortHeader("Duration"),
    meta: { align: "right" },
    cell: ({ row }) =>
      fmtDuration(
        row.original.duration?.unit === "seconds" ? row.original.duration.value : undefined,
      ),
  },
  {
    accessorKey: "session_rpe",
    header: sortHeader("RPE"),
    meta: { align: "right" },
    cell: ({ row }) => fmtInt(row.original.session_rpe ?? undefined),
  },
];

export function ActivitiesTable({ activities }: { activities: TrainingSession[] }) {
  const [sorting, setSorting] = useState<SortingState>([{ id: "local_start", desc: true }]);
  const table = useReactTable({
    data: activities,
    columns,
    state: { sorting },
    onSortingChange: setSorting,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
  });

  return (
    <div className="rounded-lg border">
      <Table>
        <TableHeader>
          {table.getHeaderGroups().map((group) => (
            <TableRow key={group.id}>
              {group.headers.map((header) => (
                <TableHead
                  key={header.id}
                  className={cn(header.column.columnDef.meta?.align === "right" && "text-right")}
                >
                  {header.isPlaceholder
                    ? null
                    : flexRender(header.column.columnDef.header, header.getContext())}
                </TableHead>
              ))}
            </TableRow>
          ))}
        </TableHeader>
        <TableBody>
          {table.getRowModel().rows.map((row) => (
            <TableRow key={row.id}>
              {row.getVisibleCells().map((cell) => (
                <TableCell
                  key={cell.id}
                  className={cn(
                    cell.column.columnDef.meta?.align === "right" && "text-right tabular-nums",
                  )}
                >
                  {flexRender(cell.column.columnDef.cell, cell.getContext())}
                </TableCell>
              ))}
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}
