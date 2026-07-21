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
import type { Activity } from "@/lib/coach-data";
import { fmtDuration, fmtInt, fmtKm, fmtNum, fmtShortDate } from "@/lib/format";
import { cn } from "@/lib/utils";

declare module "@tanstack/react-table" {
  interface ColumnMeta<TData, TValue> {
    align?: "left" | "right";
  }
}

function sortHeader(label: string) {
  return ({ column }: { column: Column<Activity, unknown> }) => (
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

const columns: ColumnDef<Activity>[] = [
  {
    accessorKey: "start_local",
    header: sortHeader("Date"),
    cell: ({ row }) => (
      <span className="whitespace-nowrap">{fmtShortDate(row.original.start_local)}</span>
    ),
  },
  {
    accessorKey: "name",
    header: "Activity",
    cell: ({ row }) => (
      <span className="block max-w-[280px] truncate font-medium">{row.original.name}</span>
    ),
  },
  {
    accessorKey: "type",
    header: "Sport",
    cell: ({ row }) =>
      row.original.type ? (
        <Badge variant="outline" className="capitalize">
          {row.original.type}
        </Badge>
      ) : (
        "—"
      ),
  },
  {
    accessorKey: "distance_m",
    header: sortHeader("Distance"),
    meta: { align: "right" },
    cell: ({ row }) => fmtKm(row.original.distance_m),
  },
  {
    accessorKey: "duration_s",
    header: sortHeader("Duration"),
    meta: { align: "right" },
    cell: ({ row }) => fmtDuration(row.original.duration_s),
  },
  {
    accessorKey: "avg_hr",
    header: sortHeader("Avg HR"),
    meta: { align: "right" },
    cell: ({ row }) => fmtInt(row.original.avg_hr),
  },
  {
    accessorKey: "elevation_gain_m",
    header: sortHeader("Elev"),
    meta: { align: "right" },
    cell: ({ row }) =>
      row.original.elevation_gain_m == null ? "—" : `${fmtInt(row.original.elevation_gain_m)} m`,
  },
  {
    accessorKey: "training_effect_aerobic",
    header: sortHeader("TE aer"),
    meta: { align: "right" },
    cell: ({ row }) => fmtNum(row.original.training_effect_aerobic),
  },
];

export function ActivitiesTable({ activities }: { activities: Activity[] }) {
  const [sorting, setSorting] = useState<SortingState>([{ id: "start_local", desc: true }]);
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
